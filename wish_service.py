"""
祈愿试炼（组队）业务逻辑。

模型（要点在 `db_manager` 的祈愿试炼一节有更细的说明）：

- 队伍满员即自动「发车」，每位成员获得一条**以队名命名的状态**，到期时间 = 队伍的
  `expire_at`（发车日 + `wish_status_days`）。
- 发车后到 `expire_at` 之前这段窗口里，**名单是活的**：补位、移出、换人都要同步状态。
- 名额按 `slot_date`（= 开团日 + 状态天数）的星期算，目的是控制节奏（本群每天一场）。
  **从不解析队名**——队名由系统生成、诸神可改，规则必须与名字无关。

本模块只出文案与结果码，不 import AstrBot，也不负责发送（mixin 负责送到哪里）。

回执与播报分成两个通道：`reply` 回给发起者，`broadcasts` 发到群里。两者都由这里
决定内容——因为「该说什么」是业务，而「发到哪里」是通道。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional, Tuple

from astrbot_plugin_faith_ladder.db_manager import (
    BEIJING_TZ,
    STATUS_SOURCE_WISH,
    WISH_DEPARTED,
    WISH_DISBANDED,
    WISH_RECRUITING,
    WISH_VOIDED,
)
from astrbot_plugin_faith_ladder.messages import PLAYER_NOT_FOUND
from astrbot_plugin_faith_ladder.plugin_config import cfg_get
from astrbot_plugin_faith_ladder.wish_messages import pick_wish_line

# 周一 … 周日（datetime.weekday() 的 0..6）
WEEKDAY_LABELS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# 队伍至少两人：一人成不了「一起被记住」
MIN_CAPACITY = 2

# 队伍状态 → 展示名
STATUS_LABELS = {
    WISH_RECRUITING: "招募中",
    WISH_DEPARTED: "已发车",
    WISH_DISBANDED: "已解散",
    WISH_VOIDED: "未能成行",
}


def _utc_stamp(when: datetime) -> str:
    return when.strftime("%Y-%m-%d %H:%M:%S")


def _utc_cutoff(minutes: int) -> str:
    """minutes 分钟之前的 UTC 时间戳（用于超时/提醒的判定列）。"""
    return _utc_stamp(datetime.now(timezone.utc) - timedelta(minutes=minutes))


@dataclass
class WishOutcome:
    """一次操作的结果：回执（给发起者）+ 播报（给群里）。"""

    ok: bool
    code: str
    reply: str = ""
    broadcasts: List[str] = field(default_factory=list)


class WishService:
    """祈愿试炼的玩家侧、诸神侧操作与调度入口。"""

    def __init__(self, db, get_config: Callable[[], dict]):
        """注入数据库与配置读取器。

        get_config 返回 AstrBot 的配置 dict；类型转换与默认值一律走 `cfg_get`
        （schema 是唯一事实来源），所以这里不重复写 int()/str() 兜底。
        """
        self.db = db
        self._get_config = get_config

    # ── 配置 ──

    def _cfg(self, key: str):
        return cfg_get(self._get_config() or {}, key)

    def _int_cfg(self, key: str, fallback: int, minimum: int) -> int:
        try:
            value = int(self._cfg(key))
        except (TypeError, ValueError):
            return fallback
        return max(minimum, value)

    def default_capacity(self) -> int:
        return self._int_cfg("wish_default_capacity", 6, MIN_CAPACITY)

    def max_capacity(self) -> int:
        return self._int_cfg("wish_max_capacity", 12, MIN_CAPACITY)

    def name_max_length(self) -> int:
        return self._int_cfg("wish_name_max_length", 16, 1)

    def status_days(self) -> int:
        return self._int_cfg("wish_status_days", 3, 1)

    def status_keep(self) -> int:
        return self._int_cfg("wish_status_keep", 3, 1)

    def list_limit(self) -> int:
        """本群同时允许的招募中队伍数（同时也是大厅展示上限）。"""
        return self._int_cfg("wish_list_limit", 2, 1)

    def per_player_create_limit(self) -> int:
        """每人每天开团次数；0 = 不限。"""
        return self._int_cfg("wish_daily_create_limit", 1, 0)

    def per_group_create_limit(self) -> int:
        """本群每天开团次数；0 = 不限。"""
        return self._int_cfg("wish_daily_group_create_limit", 4, 0)

    def reminder_minutes(self) -> int:
        """缺人提醒间隔；0 = 不提醒。"""
        return self._int_cfg("wish_reminder_minutes", 15, 0)

    def disband_minutes(self) -> int:
        """招募等待上限；0 = 不自动解散。"""
        return self._int_cfg("wish_disband_minutes", 45, 0)

    def groups(self) -> List[str]:
        raw = self._cfg("wish_groups") or []
        return [str(g).strip() for g in raw if str(g).strip()]

    def weekly_limits(self) -> List[int]:
        """名额表：索引 0..6 = 周一..周日，值 = 该日期允许成功发车的队伍数（0 = 不安排）。

        索引按 `slot_date` 的星期取，而 `slot_date` = 开团日 + 状态天数，所以实际
        关闭的日子比「名字里那个日期」早若干天（默认 3 天）。这是刻意的：规则管的是
        「状态什么时候到期」，而不是「今天能不能玩」。
        """
        raw = list(self._cfg("wish_weekly_limits") or [])
        limits: List[int] = []
        for item in raw[:7]:
            try:
                limits.append(max(0, int(item)))
            except (TypeError, ValueError):
                limits.append(0)
        while len(limits) < 7:
            limits.append(limits[-1] if limits else 1)
        return limits

    # ── 日期与名额 ──

    def now(self) -> datetime:
        """当前时间（北京时区）。测试覆盖此方法即可控制日期与名额。"""
        return datetime.now(BEIJING_TZ)

    def slot_date_of(self, now: Optional[datetime] = None) -> str:
        """名额归属日期 = 开团日 + 状态天数。"""
        return ((now or self.now()) + timedelta(days=self.status_days())).strftime("%Y-%m-%d")

    def create_date_of(self, now: Optional[datetime] = None) -> str:
        """开团日（只管两项每日开团次数上限）。"""
        return (now or self.now()).strftime("%Y-%m-%d")

    def slot_limit(self, slot_date: str) -> int:
        weekday = datetime.strptime(slot_date, "%Y-%m-%d").weekday()
        return self.weekly_limits()[weekday]

    def default_team_name(self, slot_date: str) -> str:
        """默认队名：`%m月%d日` + 祈愿试炼（沿用仓库里唯一的用户可见日期格式）。"""
        day = datetime.strptime(slot_date, "%Y-%m-%d")
        return f"{day.strftime('%m月%d日')}祈愿试炼"

    @staticmethod
    def format_slot(slot_date: str) -> str:
        day = datetime.strptime(slot_date, "%Y-%m-%d")
        return f"{day.strftime('%m月%d日')}（{WEEKDAY_LABELS[day.weekday()]}）"

    def closed_slot_weekdays(self) -> List[str]:
        """名额表里被关闭的星期（用于文案）。

        从配置现算而不是写死：关的是哪几天由 `wish_weekly_limits` 决定，文案必须
        跟着它走——否则改了名额表而文案不变，玩家会照着错的规则理解。
        """
        return [
            WEEKDAY_LABELS[index]
            for index, limit in enumerate(self.weekly_limits())
            if limit <= 0
        ]

    def closed_weekdays_text(self) -> str:
        """「周三、周六」这样的关闭星期列表；一个都没关时返回空串。"""
        return "、".join(self.closed_slot_weekdays())

    def next_open_day(self, now: Optional[datetime] = None, horizon: int = 14) -> Optional[str]:
        """从明天起往后找第一个「可以开团」的日子（YYYY-MM-DD）；找不到返回 None。

        每个日历日对应唯一的名额日期（= 该日 + 状态天数），所以「这一天能不能开团」
        只取决于那一个 slot_date 的星期，不会因为名额已被别人用掉而失效——
        作为「下一次」的答案它是准的。
        """
        base = now or self.now()
        for offset in range(1, horizon + 1):
            day = base + timedelta(days=offset)
            if self.slot_limit(self.slot_date_of(day)) > 0:
                return day.strftime("%Y-%m-%d")
        return None

    def next_open_hint(self, now: Optional[datetime] = None) -> str:
        """「下一次可开团的日子」的文案片段；连找 14 天都没有（名额表全 0）时留空。"""
        nxt = self.next_open_day(now)
        return f"\n下一次可开团的日子：{self.format_slot(nxt)}" if nxt else ""

    async def slot_quota(self, group_id: str, slot_date: str) -> Tuple[int, int]:
        """该名额日期的（上限, 已用）。"""
        return self.slot_limit(slot_date), await self.db.slot_usage(group_id, slot_date)

    @staticmethod
    def team_is_joinable(team: dict) -> bool:
        """还能进人的队伍：招募中的，或窗口未关且没满员的已发车队伍。"""
        if len(team["members"]) >= team["capacity"]:
            return False
        if team["status"] == WISH_RECRUITING:
            return True
        if team["status"] != WISH_DEPARTED:
            return False
        expire_at = team.get("expire_at")
        return bool(expire_at) and expire_at > _utc_stamp(datetime.now(timezone.utc))

    # ── 文案 ──

    def _line(self, kind: str, **kwargs) -> str:
        template = pick_wish_line(kind, self._get_config())
        if not template:
            return ""
        try:
            return template.format(**kwargs)
        except (KeyError, IndexError):
            # 自定义文案里写了不认识的占位符：退回内置池，别把播报整条丢掉
            return template

    @staticmethod
    def _members_text(team: dict) -> str:
        return "、".join(m["player_name"] for m in team["members"])

    @staticmethod
    def _team_line(team: dict) -> str:
        """一行队伍摘要：队名 + 人数/容量 + 队长 + 状态。"""
        return (
            f"{team['name']}  {len(team['members'])}/{team['capacity']}"
            f"  队长：{team['leader_name']}"
        )

    @staticmethod
    def _quota_text(slot_date: str, limit: int, used: int) -> str:
        label = WishService.format_slot(slot_date)
        if limit <= 0:
            return f"{label} 不安排试炼：本群不能开团（名额 0）"
        return f"{label} 的名额：{limit} 支，已用 {used} 支"

    def _open_status_line(self, slot_date: str, limit: int, used: int) -> str:
        """大厅里那句直白的「今天能不能开团」。

        玩家问的是今天，而名额挂在 3 天后的日期上——不直说就得他自己算，所以这里
        直接把结论给出来，再附上名额细节。
        """
        if limit <= 0:
            closed = self.closed_weekdays_text()
            reason = f"本群不安排{closed}的试炼" if closed else "本群没有安排试炼的名额"
            return f"今天不能开团（{reason}）{self.next_open_hint()}".replace("\n", "；")
        if used >= limit:
            return (
                f"今天不能开团（{self.format_slot(slot_date)} 的名额已用尽）"
                f"{self.next_open_hint()}".replace("\n", "；")
            )
        return f"今天可以开团（{self._quota_text(slot_date, limit, used)}）"

    def _depart_broadcast(self, team: dict) -> str:
        return self._line(
            "depart", team=team["name"], members=self._members_text(team),
            days=self.status_days(), leader=team["leader_name"],
        )

    def _voided_broadcast(self, team: dict) -> str:
        return self._line(
            "voided", team=team["name"], members=self._members_text(team),
            slot=self.format_slot(team["slot_date"]),
        )

    def _open_broadcast(self, team: dict) -> str:
        return self._line(
            "open", team=team["name"], leader=team["leader_name"],
            count=len(team["members"]), capacity=team["capacity"],
            missing=team["capacity"] - len(team["members"]),
        )

    @staticmethod
    def join_preference(team: dict) -> tuple:
        """不加队名时的挑队顺序：先招募中的队伍，再差得最少的，最后最早开团的。

        为什么是「差得最少」而不是「人最少」：名额按**队伍**算、且只有发车才消耗，
        所以这个玩法的目标是「至少有一支队伍开成」。按人最少挑会让人在两支队之间
        交替流动——各 1 人时来 8 个人，会补成 5/6 和 5/6，**两支都发不了车、双双
        超时**；按差得最少挑，同样这 8 个人里前 5 个就能把一支填满并发车。
        """
        return (
            team["status"] != WISH_RECRUITING,
            team["capacity"] - len(team["members"]),
            team["team_id"],
        )

    # ── 玩家侧 ──

    async def create(
        self, group_id: str, player_id: str, player_name: str,
        capacity: Optional[int] = None,
    ) -> WishOutcome:
        """开团：名字由系统生成，开团者自动入座。"""
        now = self.now()
        slot_date = self.slot_date_of(now)
        create_date = self.create_date_of(now)

        limit, used = await self.slot_quota(group_id, slot_date)
        if limit <= 0:
            closed = self.closed_weekdays_text()
            return WishOutcome(
                False, "closed_day",
                f"今天不能开团：队名日期会落在"
                f"{WEEKDAY_LABELS[datetime.strptime(slot_date, '%Y-%m-%d').weekday()]}，"
                f"而本群不安排{closed}的祈愿试炼。"
                f"{self.next_open_hint(now)}\n"
                f"（名额看的是队名里的日期，队名日期 = 开团日 + {self.status_days()} 天）",
            )
        if used >= limit:
            return WishOutcome(
                False, "no_slot",
                f"{self._quota_text(slot_date, limit, used)}\n"
                f"名额已用尽，今天开团也发不了车。{self.next_open_hint(now)}",
            )

        if capacity is None:
            capacity = self.default_capacity()
        if not isinstance(capacity, int) or capacity < MIN_CAPACITY:
            return WishOutcome(False, "bad_capacity", f"人数至少 {MIN_CAPACITY} 人。")
        if capacity > self.max_capacity():
            return WishOutcome(
                False, "bad_capacity", f"人数最多 {self.max_capacity()} 人。"
            )

        team_id, code = await self.db.create_wish_team(
            group_id, self.default_team_name(slot_date), capacity, player_id, player_name,
            slot_date, create_date,
            recruiting_limit=self.list_limit(),
            per_player_limit=self.per_player_create_limit(),
            per_group_limit=self.per_group_create_limit(),
        )
        if team_id is None:
            return WishOutcome(False, code, self._create_failure(code))

        team = await self.db.get_wish_team(team_id)
        limit, used = await self.slot_quota(group_id, slot_date)
        reply = (
            f"已开团：{self._team_line(team)}\n"
            f"缺 {team['capacity'] - len(team['members'])} 人满员即发车，"
            f"满员后全员获得「{team['name']}」状态（{self.status_days()} 天）。\n"
            f"{self._quota_text(slot_date, limit, used)}"
        )
        broadcast = self._open_broadcast(team)
        return WishOutcome(True, "ok", reply, [broadcast] if broadcast else [])

    @staticmethod
    def _create_failure(code: str) -> str:
        return {
            "already_in_team": "你已经有队伍在招募了。先「祈愿退出」再来开团。",
            "too_many_teams": "本群已有队伍在招募，等它发车或解散后再开。（不会占用你今天的开团次数）",
            "daily_limit": "你今天已经开过一次团了，明天再来。",
            "group_daily_limit": "本群今天的开团次数已经用完。",
            "name_taken": "队名已被占用（同名队伍太多），请诸神改名后再试。",
        }.get(code, f"开团失败（{code}）。")

    async def join(
        self, group_id: str, player_id: str, player_name: str,
        team_name: Optional[str] = None,
    ) -> WishOutcome:
        """加入队伍：给了队名就找那一支，没给就补最接近满员的那支。"""
        if team_name:
            team = await self.db.get_wish_team_by_name(group_id, team_name.strip())
            if team is None:
                return WishOutcome(False, "not_found", f"本群没有叫「{team_name}」的队伍。")
        else:
            candidates = [
                t for t in await self.db.list_wish_teams(
                    group_id, (WISH_RECRUITING, WISH_DEPARTED)
                )
                if self.team_is_joinable(t)
            ]
            if not candidates:
                return WishOutcome(
                    False, "no_candidates",
                    "本群现在没有可加入的队伍。可以自己开一支：祈愿组队",
                )
            team = min(candidates, key=self.join_preference)

        return await self._join_team(group_id, team, player_id, player_name)

    async def _join_team(
        self, group_id: str, team: dict, player_id: str, player_name: str
    ) -> WishOutcome:
        """加入的核心：调 DB 层并按结果码出文案。诸神「补位」也走这里。"""
        code, updated = await self.db.join_wish_team(
            team["team_id"], group_id, player_id, player_name,
            slot_limit=self.slot_limit(team["slot_date"]),
            status_days=self.status_days(),
            keep_statuses=self.status_keep(),
        )
        team = updated or team

        if code == "joined":
            return WishOutcome(
                True, code,
                f"已加入 {self._team_line(team)}\n"
                f"缺 {team['capacity'] - len(team['members'])} 人，满员即发车。",
            )
        if code == "replenished":
            return WishOutcome(
                True, code,
                f"已补位到【{team['name']}】（{len(team['members'])}/{team['capacity']}）\n"
                f"你获得状态「{team['name']}」，与队友同一天到期（{team['expire_at']} UTC）。",
            )
        if code == "departed":
            reply = (
                f"{team['name']} 满员发车！\n"
                f"成员：{self._members_text(team)}\n"
                f"全员获得状态「{team['name']}」，至 {team['expire_at']}（UTC）。"
            )
            broadcasts = [self._depart_broadcast(team)]
            for loser in await self.db.void_sibling_teams(group_id, team["slot_date"]):
                text = self._voided_broadcast(loser)
                if text:
                    broadcasts.append(text)
            return WishOutcome(True, code, reply, broadcasts)
        if code == "already_member":
            return WishOutcome(True, code, f"你已经在【{team['name']}】里了。")
        if code == "full":
            return WishOutcome(False, code, f"【{team['name']}】已经满了。")
        if code == "name_conflict":
            return WishOutcome(
                False, code,
                f"你身上已有名为「{team['name']}」的状态，不能加入同名队伍。\n"
                f"可以换一支队伍加入、等那条状态过期，或请诸神用「重命名状态」把它改掉。",
            )
        if code == "already_in_slot":
            return WishOutcome(
                False, code,
                f"你已经参加过 {self.format_slot(team['slot_date'])} 那场试炼了"
                f"（同一个日期只能参与一次）。",
            )
        if code == "already_in_other_team":
            return WishOutcome(
                False, code, "你已经有队伍了。先「祈愿退出」再加入别的队伍。"
            )
        if code == "no_slot":
            return WishOutcome(
                False, code,
                f"【{team['name']}】对应的 {self.format_slot(team['slot_date'])} "
                f"名额已经用尽，加入了也发不了车。",
            )
        if code == "window_closed":
            return WishOutcome(
                False, code, f"【{team['name']}】的名单已经定下了（3 天窗口已过）。"
            )
        if code == "voided":
            return WishOutcome(False, code, f"【{team['name']}】未能成行，不能再加入。")
        if code == WISH_DISBANDED:
            return WishOutcome(False, code, f"【{team['name']}】已经解散了。")
        return WishOutcome(False, code, f"加入失败（{code}）。")

    async def leave(self, group_id: str, player_id: str) -> WishOutcome:
        """退出：队长退出即解散（他不能把队伍留给别人）。"""
        code, team = await self.db.leave_wish_team(group_id, player_id)
        if code == "left":
            return WishOutcome(True, code, f"已退出【{team['name']}】。")
        if code == "disbanded":
            return WishOutcome(
                True, code, f"你是队长，「{team['name']}」已随你的退出解散。"
            )
        return WishOutcome(
            False, code,
            "你当前没有招募中的队伍可退。\n"
            "（已发车的队伍名单由诸神调整：祈愿管理 移出 / 换人）",
        )

    async def list_open(self, group_id: str) -> WishOutcome:
        """大厅：本群招募中的队伍 + 今日名额。"""
        teams = await self.db.get_open_wish_teams(group_id)
        # 按「不加队名时的挑队顺序」展示：排在最前的那支就是会被优先补齐的，
        # 否则玩家看到的第 [1] 支与实际会被填的那支不一致
        teams = sorted(teams, key=self.join_preference)
        slot_date = self.slot_date_of()
        limit, used = await self.slot_quota(group_id, slot_date)

        lines = ["祈愿试炼 · 本群招募中", ""]
        if not teams:
            lines.append("（暂时没有队伍在招募）")
        for index, team in enumerate(teams, start=1):
            missing = team["capacity"] - len(team["members"])
            lines.append(
                f"[{index}] {team['name']}  {len(team['members'])}/{team['capacity']}"
                f"  缺{missing}人  队长：{team['leader_name']}"
            )
            lines.append(f"     成员：{self._members_text(team)}")
        lines += [
            "",
            self._open_status_line(slot_date, limit, used),
            "参与：祈愿组队 [人数] ／ 祈愿加入 [队名] ／ 祈愿我的",
            "（不加队名时补进最接近满员的那支，也就是排在最前面的那支）",
        ]
        return WishOutcome(True, "ok", "\n".join(lines))

    async def mine(self, group_id: str, player_id: str) -> WishOutcome:
        """我的队伍：招募中或窗口未关的已发车队伍。"""
        team = await self.db.get_player_active_wish_team(group_id, player_id)
        if team is None:
            history = [
                s for s in await self.db.get_player_statuses(group_id, player_id)
                if s.get("source") == STATUS_SOURCE_WISH
            ]
            lines = ["你当前没有参加祈愿试炼。"]
            if history:
                lines.append("")
                lines.append("仍在生效的试炼状态：")
                for status in history:
                    lines.append(f"  {status['status_name']}：剩余{status['remaining_days']}天")
            return WishOutcome(False, "not_found", "\n".join(lines))

        role = "队长" if team["leader_id"] == player_id else "队员"
        lines = [
            f"{STATUS_LABELS.get(team['status'], team['status'])} · {team['name']}",
            f"{len(team['members'])}/{team['capacity']}  你是{role}  队长：{team['leader_name']}",
            f"成员：{self._members_text(team)}",
        ]
        if team["status"] == WISH_DEPARTED:
            lines.append(f"状态到期：{team['expire_at']}（UTC）")
            lines.append("名单在到期前仍可由诸神调整；有空位时任何人都能补位。")
        else:
            lines.append("等满员自动发车。")
        return WishOutcome(True, "ok", "\n".join(lines))

    async def random_self(
        self, group_id: str, player_id: str, player_name: str
    ) -> WishOutcome:
        """随机匹配：把自己丢进最缺人的一支。

        仅当未组队、或自己的队伍只有 1 人时可用——避免把别人从成型的队伍里拽走
        （个人随机不该动到别人的名单）。
        """
        current = await self.db.get_player_active_wish_team(group_id, player_id)
        if current is not None and len(current["members"]) > 1:
            return WishOutcome(
                False, "in_team",
                f"你已经在【{current['name']}】里了（{len(current['members'])} 人）。\n"
                f"随机匹配只在「未组队 / 队伍只有你一人」时可用。",
            )

        candidates = [
            t for t in await self.db.list_wish_teams(group_id, (WISH_RECRUITING,))
            if self.team_is_joinable(t) and not any(
                m["player_id"] == player_id for m in t["members"]
            )
        ]
        if not candidates:
            return WishOutcome(False, "no_candidates", "本群没有可补齐的队伍。可以自己开一支：祈愿组队")

        team = min(candidates, key=self.join_preference)
        if current is not None:
            # 自己的独苗队伍先解散，否则「一人一队」会把自己挡在门外
            await self.db.leave_wish_team(group_id, player_id)
        return await self._join_team(group_id, team, player_id, player_name)

    # ── 诸神侧 ──

    async def _find_team(self, group_id: str, team_name: str):
        team = await self.db.get_wish_team_by_name(group_id, (team_name or "").strip())
        return team

    async def admin_list(self, group_id: str) -> WishOutcome:
        """列表：招募中 + 已发车 + 未能成行（**不含已解散**）。"""
        teams = await self.db.list_wish_teams(
            group_id, (WISH_RECRUITING, WISH_DEPARTED, WISH_VOIDED)
        )
        lines = ["祈愿试炼 · 本群队伍（不含已解散）", ""]
        if not teams:
            lines.append("（暂无记录）")
        for team in teams:
            label = STATUS_LABELS.get(team["status"], team["status"])
            extra = ""
            if team["status"] == WISH_DEPARTED and team["expire_at"]:
                extra = f"  到期：{team['expire_at']}"
            lines.append(
                f"{team['name']}  {len(team['members'])}/{team['capacity']}"
                f"  {label}  队长：{team['leader_name']}{extra}"
            )
            lines.append(f"   成员：{self._members_text(team) or '（空）'}")
        lines += ["", "「祈愿管理 已解散」查看已解散的队伍"]
        return WishOutcome(True, "ok", "\n".join(lines))

    async def admin_list_disbanded(self, group_id: str) -> WishOutcome:
        """已解散：单独查看。"""
        teams = await self.db.list_wish_teams(group_id, (WISH_DISBANDED,))
        lines = ["祈愿试炼 · 已解散的队伍", ""]
        if not teams:
            lines.append("（暂无记录）")
        for team in teams:
            lines.append(
                f"{team['name']}  {len(team['members'])}/{team['capacity']}"
                f"  队长：{team['leader_name']}"
            )
            lines.append(f"   成员：{self._members_text(team) or '（空）'}")
        return WishOutcome(True, "ok", "\n".join(lines))

    async def admin_roster(
        self, group_id: str, team_name: str,
        ladder_delta: int = 0, pilgrimage_delta: int = 0,
    ) -> WishOutcome:
        """名单：给分数就输出可直接粘进「批量录入」的文本，不给就只列队员名。"""
        team = await self._find_team(group_id, team_name)
        if team is None:
            return WishOutcome(False, "not_found", f"本群没有叫「{team_name}」的队伍。")
        names = [m["player_name"] for m in team["members"]]
        if not names:
            return WishOutcome(False, "empty", f"【{team['name']}】没有成员。")

        header = (
            f"{team['name']}  {len(names)}/{team['capacity']}"
            f"  {STATUS_LABELS.get(team['status'], team['status'])}"
        )
        if ladder_delta == 0 and pilgrimage_delta == 0:
            # 两个都 0 的话批量录入会把每个块都丢掉（它要求块内至少有一项变化），
            # 所以退回纯名单，别给出一段粘进去必定报错的文本
            return WishOutcome(
                True, "plain",
                header + "\n\n" + "\n".join(names)
                + "\n\n（要生成可直接粘贴进「批量录入」的文本，"
                  "请带上分数：祈愿管理 名单 <队名> <登神分> <觐见分>）",
            )

        blocks = []
        for name in names:
            parts = [f"【玩家：{name}】"]
            if ladder_delta:
                parts.append(f"【登神之路{ladder_delta:+d}】")
            if pilgrimage_delta:
                parts.append(f"【觐见之梯{pilgrimage_delta:+d}】")
            blocks.append("".join(parts))
        return WishOutcome(
            True, "batch",
            header
            + "\n\n复制下面整段发给「批量录入」（分数已填好，可自行改数）：\n"
            + "\n".join(blocks),
        )

    async def admin_disband(self, group_id: str, team_name: str) -> WishOutcome:
        """解散；已发车的队伍连带撤销全队状态。"""
        team = await self._find_team(group_id, team_name)
        if team is None:
            return WishOutcome(False, "not_found", f"本群没有叫「{team_name}」的队伍。")

        code, updated = await self.db.disband_wish_team(team["team_id"], group_id)
        if code != "ok":
            return WishOutcome(False, code, f"【{team['name']}】已经是{STATUS_LABELS.get(team['status'], team['status'])}了。")
        revoked = f"，并撤销了 {len(updated['members'])} 人的状态" if team["status"] == WISH_DEPARTED else ""
        return WishOutcome(True, "ok", f"已解散【{team['name']}】{revoked}。")

    async def admin_rename(
        self, group_id: str, team_name: str, new_name: str
    ) -> WishOutcome:
        """重命名（诸神是唯一的命名途径）；已发车队伍连带改成员状态名。"""
        new_name = (new_name or "").strip()
        if not new_name:
            return WishOutcome(False, "empty_name", "新队名不能为空。")
        if len(new_name) > self.name_max_length():
            return WishOutcome(
                False, "too_long", f"队名最长 {self.name_max_length()} 个字符。"
            )

        team = await self._find_team(group_id, team_name)
        if team is None:
            return WishOutcome(False, "not_found", f"本群没有叫「{team_name}」的队伍。")

        code, detail = await self.db.rename_wish_team(team["team_id"], group_id, new_name)
        if code == "name_taken":
            return WishOutcome(False, code, f"本群已经有叫「{new_name}」的队伍了。")
        if code != "ok":
            return WishOutcome(False, code, f"改名失败（{code}）。")

        note = ""
        if detail["renamed"] or detail["merged"]:
            note = (
                f"\n已同步成员状态名：改名 {detail['renamed']} 人"
                f"（其中 {detail['merged']} 人与原有同名状态合并，到期时间取更晚的、阻断项保留原有的）"
            )
        if detail["skipped"]:
            note += f"\n有 {detail['skipped']} 人身上没有这条状态，已跳过。"
        return WishOutcome(True, "ok", f"已把【{team_name}】改名为「{new_name}」。{note}")

    async def admin_remove_member(
        self, group_id: str, team_name: str, player_name: str
    ) -> WishOutcome:
        """移出成员：撤销他因这支队拿到的状态。"""
        team = await self._find_team(group_id, team_name)
        if team is None:
            return WishOutcome(False, "not_found", f"本群没有叫「{team_name}」的队伍。")

        target = await self.db.get_player_by_name(group_id, player_name)
        if target is None:
            return WishOutcome(False, "player_not_found", PLAYER_NOT_FOUND.format(name=player_name))

        code, updated = await self.db.remove_wish_team_member(
            team["team_id"], group_id, target.player_id
        )
        if code == "not_member":
            return WishOutcome(False, code, f"{player_name} 不在【{team['name']}】里。")
        if code != "ok":
            return WishOutcome(False, code, f"移出失败（{code}）。")

        lines = [f"已把 {player_name} 移出【{updated['name']}】，并撤销其状态。"]
        if updated["leader_id"] != team["leader_id"]:
            lines.append(f"队长已交给 {updated['leader_name']}。")
        if updated["status"] == WISH_DISBANDED:
            lines.append("队伍已空，随之解散。")
        else:
            lines.append(f"现在 {len(updated['members'])}/{updated['capacity']}，空位可由任何人补上：祈愿加入 {updated['name']}")
        return WishOutcome(True, "ok", "\n".join(lines))

    async def admin_fill_member(
        self, group_id: str, team_name: str, player_name: str
    ) -> WishOutcome:
        """补位：把人强行塞进队伍，满员则照常发车并挂状态。"""
        team = await self._find_team(group_id, team_name)
        if team is None:
            return WishOutcome(False, "not_found", f"本群没有叫「{team_name}」的队伍。")

        target = await self.db.get_player_by_name(group_id, player_name)
        if target is None:
            return WishOutcome(False, "player_not_found", PLAYER_NOT_FOUND.format(name=player_name))

        return await self._join_team(group_id, team, target.player_id, target.player_name)

    async def admin_swap(
        self, group_id: str, team_name: str, out_name: str, in_name: str
    ) -> WishOutcome:
        """换人：把 out_name 的房间状态转移到 in_name 身上。"""
        team = await self._find_team(group_id, team_name)
        if team is None:
            return WishOutcome(False, "not_found", f"本群没有叫「{team_name}」的队伍。")

        out_player = await self.db.get_player_by_name(group_id, out_name)
        if out_player is None:
            return WishOutcome(False, "player_not_found", PLAYER_NOT_FOUND.format(name=out_name))
        in_player = await self.db.get_player_by_name(group_id, in_name)
        if in_player is None:
            return WishOutcome(False, "player_not_found", PLAYER_NOT_FOUND.format(name=in_name))

        code, updated = await self.db.swap_wish_team_member(
            team["team_id"], group_id, out_player.player_id, in_player.player_id,
            in_player.player_name, keep_statuses=self.status_keep(),
        )
        if code == "same_player":
            return WishOutcome(False, code, "被换下与接替的是同一个人。")
        if code == "not_member":
            return WishOutcome(False, code, f"{out_name} 不在【{team['name']}】里。")
        if code == "already_member":
            return WishOutcome(False, code, f"{in_name} 已经在这支队里了。")
        if code == "in_has_team":
            return WishOutcome(
                False, code, f"{in_name} 已经有别的队伍了，先移出再换。"
            )
        if code == "in_in_slot":
            return WishOutcome(
                False, code,
                f"{in_name} 已经参加过 {self.format_slot(team['slot_date'])} 那场试炼了。",
            )
        if code == "in_name_conflict":
            return WishOutcome(
                False, code, f"{in_name} 身上已有名为「{team['name']}」的状态。"
            )
        if code != "ok":
            return WishOutcome(False, code, f"换人失败（{code}）。")

        lines = [f"已把【{updated['name']}】的 {out_name} 换成 {in_name}。"]
        if updated["status"] == WISH_DEPARTED and updated["expire_at"]:
            lines.append(
                f"{in_name} 获得状态「{updated['name']}」，到期时间与队友一致"
                f"（{updated['expire_at']} UTC）。"
            )
        lines.append(f"名单：{self._members_text(updated)}")
        # "in" 是 Python 关键字，不能写成 in=…，只能用字典展开传（文案里的占位符保持 {in}）
        broadcast = self._line(
            "swap", **{"team": updated["name"], "out": out_name, "in": in_name}
        )
        return WishOutcome(True, "ok", "\n".join(lines), [broadcast] if broadcast else [])

    async def admin_swap_players(
        self, group_id: str, out_name: str, in_name: str
    ) -> WishOutcome:
        """换人（不带队名）：由「被换下的人」所在的那支队伍决定是哪一支。

        一个人同时只可能在一支进行中的队伍里（一人一队），所以这里不会有歧义——
        诸神不必先查队名。
        """
        out_player = await self.db.get_player_by_name(group_id, out_name)
        if out_player is None:
            return WishOutcome(False, "player_not_found", PLAYER_NOT_FOUND.format(name=out_name))

        team = await self.db.get_player_active_wish_team(group_id, out_player.player_id)
        if team is None:
            return WishOutcome(
                False, "not_member", f"{out_name} 不在任何进行中的队伍里。"
            )
        return await self.admin_swap(group_id, team["name"], out_name, in_name)

    async def admin_extend(
        self, group_id: str, team_name: str, days: int
    ) -> WishOutcome:
        """延期：改队伍的到期时间，全队状态同步到新值。"""
        team = await self._find_team(group_id, team_name)
        if team is None:
            return WishOutcome(False, "not_found", f"本群没有叫「{team_name}」的队伍。")

        code, updated = await self.db.extend_wish_team_expiry(
            team["team_id"], group_id, days, keep_statuses=self.status_keep()
        )
        if code == "invalid_days":
            return WishOutcome(False, code, "天数必须是正整数（延期只延不缩）。")
        if code == "not_departed":
            return WishOutcome(False, code, "这支队伍还没发车，没有可延期的状态。")
        if code != "ok":
            return WishOutcome(False, code, f"延期失败（{code}）。")

        return WishOutcome(
            True, "ok",
            f"已把【{updated['name']}】全队状态延期 {days} 天，"
            f"新的到期时间：{updated['expire_at']}（UTC）。\n"
            f"成员：{self._members_text(updated)}",
        )

    async def admin_stats(self, group_id: str, days: int = 7) -> WishOutcome:
        """近期运行统计：开团、发车、参与，以及卡在哪一步。

        存在的理由：调参要看的是「开团多不多、发车成不成」，而这些数只有裸 SQL 能取。
        没有这个出口，「按数据调参」就只是一句愿望。
        """
        days = max(1, min(int(days or 7), 365))
        since = (self.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        data = await self.db.wish_team_stats(group_id, since)
        teams = data["teams"]
        if not teams:
            return WishOutcome(
                True, "ok",
                f"祈愿试炼 · 近 {days} 天\n\n本群没有开过团。\n"
                f"（先确认本群在 wish_groups 里，再看今天是不是关闭日："
                f"「祈愿」大厅会直接告诉你）",
            )

        def by_status(name): return [t for t in teams if t["status"] == name]

        departed = by_status(WISH_DEPARTED)
        voided = by_status(WISH_VOIDED)
        recruiting = by_status(WISH_RECRUITING)
        unfilled = by_status(WISH_DISBANDED)   # 超时/队长退出/诸神解散——都属没凑够
        finished = len(departed) + len(voided) + len(unfilled)

        joined = sum(t["members"] for t in teams)
        seats = sum(t["capacity"] for t in departed)
        filled = sum(t["members"] for t in departed)
        rate = f"{len(departed) * 100 // finished}%" if finished else "—"
        fill = f"{filled * 100 // seats}%" if seats else "—"

        lines = [
            f"祈愿试炼 · 近 {days} 天",
            "",
            f"开团 {len(teams)} 次：发车 {len(departed)}、未能成行 {len(voided)}、"
            f"没凑够 {len(unfilled)}"
            + (f"、仍在招募 {len(recruiting)}" if recruiting else ""),
            f"发车率 {rate}（已结束的 {finished} 支里）",
            f"参与 {joined} 人次，去重 {data['players']} 人",
            f"发车队伍平均满员率 {fill}",
            "",
            "怎么看：开团少 = 没人想玩（考虑给它一点产出）；"
            "开团多而发车少 = 凑不齐人（调小 wish_default_capacity，或拉长 wish_disband_minutes）。",
        ]
        return WishOutcome(True, "ok", "\n".join(lines))

    async def admin_clear(self, group_id: str) -> WishOutcome:
        """清空队伍记录，并释放当天名额（逃生口）。"""
        count = await self.db.clear_wish_teams(group_id)
        slot_date = self.slot_date_of()
        limit, used = await self.slot_quota(group_id, slot_date)
        return WishOutcome(
            True, "ok",
            f"已清空本群 {count} 支队伍记录。\n"
            f"当天名额已释放：{self._quota_text(slot_date, limit, used)}\n"
            f"（已经发出去的状态不受影响，要撤用「移除状态」）",
        )

    # ── 调度入口 ──

    async def tick(self) -> List[Tuple[str, str]]:
        """每 30 秒调用一次：解散超时队伍 + 给到点的队伍发提醒。

        返回 [(群号, 播报文案)]，由 mixin 负责送到群里（并施加群访问控制的静默规则）。
        """
        out: List[Tuple[str, str]] = []

        minutes = self.disband_minutes()
        if minutes > 0:
            for team in await self.db.disband_stale_wish_teams(_utc_cutoff(minutes)):
                if not team["members"]:
                    continue  # 空队不吵人
                if str(team["group_id"]) not in self.groups():
                    continue
                text = self._line(
                    "timeout", team=team["name"], count=len(team["members"]),
                    capacity=team["capacity"],
                )
                if text:
                    out.append((str(team["group_id"]), text))

        remind = self.reminder_minutes()
        if remind > 0:
            cutoff = _utc_cutoff(remind)
            for group_id in self.groups():
                for team in await self.db.get_open_wish_teams(group_id):
                    if len(team["members"]) >= team["capacity"]:
                        continue
                    if not await self.db.claim_wish_reminder(team["team_id"], cutoff):
                        continue  # 这个窗口已经被别的 tick 认领过了
                    limit, used = await self.slot_quota(group_id, team["slot_date"])
                    text = self._line(
                        "remind", team=team["name"], count=len(team["members"]),
                        capacity=team["capacity"],
                        missing=team["capacity"] - len(team["members"]),
                        minutes=remind,
                    )
                    if text:
                        out.append((group_id, f"{text}\n{self._quota_text(team['slot_date'], limit, used)}"))
        return out

