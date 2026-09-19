"""
神明的赌局。

调性对标《诸神愚戏》：神明从 16 位信仰中随机现身，在群里抛下一个短时赌局，
时限内完成指定动作的人即入局；开奖时神明可能兑现、也可能只是嘴上耍你——
**但永远不会真的动你的分数或道具**（除非管理员显式打开 `wager_score_enabled`）。

落点说明：
- 开局/开奖由调度器每 5 秒 tick 一次（`_wager_tick`），状态只在内存；
- 入局由现有的全消息监听器调用 `_wager_entry(event, "speak")`（说话）与
  祷词流程里的 `_wager_entry(event, "pray")`（献祷词）；
- 每次赌局只产生两条群消息（开局 + 开奖），不刷屏；
- 记录写进 `god_wagers` / `god_wager_entries` 两张表，供以后做统计。

约定：mixin 只放实现体，装饰器留在 main.py（见 commands/__init__.py）。
"""

from __future__ import annotations

import random
import time
from typing import TYPE_CHECKING, Dict, Optional

from astrbot_plugin_faith_ladder.messages import PERMISSION_DENIED
from astrbot_plugin_faith_ladder.models import VALID_FAITHS
from astrbot_plugin_faith_ladder.wager_messages import pick_wager_line

try:
    from astrbot.api import logger
except ImportError:  # 无 AstrBot 环境（如跑测试）时退回标准库日志
    import logging
    logger = logging.getLogger(__name__)

# 入局动作 → 文案里的说法（新增动作时同步这里的说明）
WAGER_ACTION_LABELS = {
    "speak": "开口说话",
    "pray": "献上祷词",
}

# 一次赌局最多记录多少名参与者（防止被刷屏塞爆内存）
MAX_ENTRIES = 200

# 开奖结果：兑现 / 嘲弄 / 嘴上反悔，各 1/3
WAGER_OUTCOMES = ("win", "lose", "tease")


class WagerMixin:
    """神明的赌局（默认关闭，按群开启）。"""

    # ── 状态与时间 ──

    def _wager_state(self) -> Dict[str, dict]:
        """群 → 进行中的赌局（宿主在 __init__ 里初始化；测试替身没有时惰性补一个）。"""
        if not hasattr(self, "_wagers"):
            self._wagers: Dict[str, dict] = {}
        return self._wagers

    def _wager_last_map(self) -> Dict[str, float]:
        if not hasattr(self, "_wager_last"):
            self._wager_last: Dict[str, float] = {}
        return self._wager_last

    def _wager_now(self) -> float:
        """当前时间（单调时钟）。测试里覆盖此方法即可控制时限。"""
        return time.monotonic()

    # ── 配置 ──

    def _wager_enabled(self) -> bool:
        return bool(self._cfg("wager_enabled"))

    def _wager_groups(self) -> list:
        groups = self._cfg("wager_groups") or []
        return [str(g).strip() for g in groups if str(g).strip()]

    def _wager_duration(self) -> int:
        try:
            return max(5, int(self._cfg("wager_duration_seconds")))
        except (TypeError, ValueError):
            return 60

    def _wager_interval_seconds(self) -> int:
        try:
            return max(1, int(self._cfg("wager_interval_minutes"))) * 60
        except (TypeError, ValueError):
            return 30 * 60

    def _wager_reward(self) -> int:
        try:
            return max(0, int(self._cfg("wager_reward")))
        except (TypeError, ValueError):
            return 3

    def _wager_scoring(self) -> bool:
        return bool(self._cfg("wager_score_enabled"))

    # ── 调度入口：每 5 秒调用一次 ──

    async def _wager_tick(self) -> None:
        if not self._wager_enabled():
            return

        now = self._wager_now()
        for group_id in self._wager_groups():
            state = self._wager_state().get(group_id)
            if state is not None:
                # 进行中的赌局照常结算（_wager_send 对被排除的群静默），
                # 否则配置一改，这条状态就永远卡在内存里、再也不会被清掉
                if now >= state["ends_at"]:
                    await self._wager_settle(group_id)
                continue

            # 被群访问控制排除的群不参与赌局：插件在那儿本该完全静默，
            # 不能因为赌局走了另一条发送通道就绕开这个约束
            if self._group_access_blocked(group_id):
                continue
            # 用 None 而不是 0.0 表示"从没开过局"：单调时钟的起点是开机时间，
            # 若拿 0.0 相减，机器刚启动不久（运行时长 < 间隔）时首次开局会被莫名推迟
            last = self._wager_last_map().get(group_id)
            if last is None or now - last >= self._wager_interval_seconds():
                text = await self._wager_announce(group_id, now)
                if text:
                    await self._wager_notify(group_id, text)

    # ── 开局 ──

    def _prayer_available(self, group_id: str) -> bool:
        """该群现在能不能触发祷词（功能开关 + 触发群列表）。

        抽到"献上祷词"却没人能触发，开奖就只能走"无人入局"——所以只在能触发时才允许抽它。
        """
        if not self._cfg("feature_prayer_enabled"):
            return False
        groups = self._cfg("prayer_trigger_groups") or []
        return str(group_id).strip() in {str(g).strip() for g in groups if str(g).strip()}

    async def _wager_announce(self, group_id: str, now: Optional[float] = None) -> str:
        """抛下一场赌局：随机神明 + 随机入局动作。返回开局文案，由调用方决定怎么发
        （自动 tick 走 _wager_send 广播；手动指令直接把这条当作回复）。"""
        now = self._wager_now() if now is None else now
        god = random.choice(list(VALID_FAITHS))
        actions = ["speak", "pray"] if self._prayer_available(group_id) else ["speak"]
        action = random.choice(actions)
        seconds = self._wager_duration()
        ends_at = now + seconds

        text = pick_wager_line(god, "announce", dict(self.config)).format(
            god=god, seconds=seconds, action=WAGER_ACTION_LABELS[action], reward="",
            winner="",
        )

        self._wager_state()[group_id] = {
            "god": god, "action": action, "ends_at": ends_at,
            "entries": {}, "wager_id": None,
        }
        self._wager_last_map()[group_id] = now
        logger.info(f"[Wager] 群 {group_id} 开局：{god} / {action} / {seconds}s")

        wager_id = await self._wager_store_open(group_id, god, action, seconds)
        self._wager_state()[group_id]["wager_id"] = wager_id
        return text

    # ── 手动开局（指令） ──

    async def _wager_open_impl(self, event):
        """手动抛下一场赌局。（注册在 main.py）

        与自动开局的差别：不看 `wager_groups`（管理员在哪个群发就在哪个群开）、
        不等间隔；其余条件相同（总开关、群未被访问控制排除、当前无进行中的赌局）。
        """
        blocked, gate_msg = await self._gate(event, None)
        if blocked:
            if gate_msg:
                yield event.plain_result(gate_msg)
            return

        if not await self._check_perm(event):
            yield event.plain_result(PERMISSION_DENIED["god_only"])
            return

        if not self._wager_enabled():
            yield event.plain_result(
                "「神明的赌局」总开关未开启（配置项 wager_enabled）。\n"
                "开启后即可用本指令在任何未被群访问控制排除的群里开局。"
            )
            return

        group_id = self._get_group_id(event)
        state = self._wager_state().get(group_id)
        if state is not None:
            remain = max(0, int(state["ends_at"] - self._wager_now()))
            yield event.plain_result(f"本群已有一场赌局正在进行（还剩 {remain} 秒）。")
            return

        text = await self._wager_announce(group_id)
        if text:
            yield event.plain_result(text)

    # ── 入局 ──

    async def _wager_entry(self, event, action: str) -> None:
        """记录一次入局。不在赌局中、动作不对、已入局都会直接返回。"""
        if not self._wager_enabled():
            return

        group_id = self._get_group_id(event)
        if self._group_access_blocked(group_id):
            return
        state = self._wager_state().get(group_id)
        if state is None or state["action"] != action:
            return
        if self._wager_now() >= state["ends_at"]:
            return
        if len(state["entries"]) >= MAX_ENTRIES:
            return

        sender_id = str(event.get_sender_id())
        if not sender_id:
            return
        self_id = ""
        if hasattr(event, "get_self_id"):
            try:
                self_id = str(event.get_self_id())
            except Exception:
                self_id = ""
        if sender_id == self_id:
            return
        if sender_id in state["entries"]:
            return

        name = event.get_sender_name() if hasattr(event, "get_sender_name") else sender_id
        state["entries"][sender_id] = str(name)
        await self._wager_store_entry(state.get("wager_id"), group_id, sender_id, str(name))

    # ── 开奖 ──

    async def _wager_settle(self, group_id: str) -> None:
        """时限到：从参与者里挑一个，宣告结果（默认不动真实分数）。"""
        state = self._wager_state().pop(group_id, None)
        if state is None:
            return

        god = state["god"]
        config = dict(self.config)
        entries = state["entries"]

        if not entries:
            text = pick_wager_line(god, "none", config).format(
                god=god, winner="", seconds="", action="", reward=""
            )
            await self._wager_store_close(state.get("wager_id"), None, None, 0)
            await self._wager_notify(group_id, text)
            return

        winner_id, winner_name = random.choice(list(entries.items()))
        outcome = random.choice(WAGER_OUTCOMES)
        scoring = self._wager_scoring()
        # 不改分时，文案里的"赏赐"只当作口头承诺（呼应神明会骗人的调性）
        reward_text = "一句口头承诺"

        if outcome == "win" and scoring and self._wager_reward() > 0:
            reward = self._wager_reward()
            reward_text = f"觐见 +{reward}"
            ok, _ = await self.ladder_service.add_score(
                group_id, winner_id, winner_name,
                ladder_delta=0, pilgrimage_delta=reward,
                operator_id="god_wager", reason="神明的赌局",
            )
            if not ok:
                logger.warning(f"[Wager] 发放奖励失败：{winner_name}")

        text = pick_wager_line(god, outcome, config).format(
            god=god, winner=winner_name, seconds="", action="", reward=reward_text
        )
        if winner_name not in text:
            # 嘲弄/反悔类的文案里常常不提赢家，但"神明点到了谁"是玩家最关心的信息，补一行
            text += f"\n—— 被点到的是 {winner_name}。"
        if not scoring:
            # 免责声明独立成行：塞进文案占位符里的话，遇到不含占位符的句子就丢了
            text += "\n（本次结果不影响实际分数）"
        await self._wager_store_close(state.get("wager_id"), winner_id, winner_name, len(entries))
        await self._wager_notify(group_id, text)
        logger.info(f"[Wager] 群 {group_id} 开奖：{god}/{outcome} 参与者 {len(entries)}")

    # ── 发送与落库（可被测试替换）──

    async def _wager_notify(self, group_id: str, text: str) -> None:
        """对外播报的统一出口：被群访问控制排除的群一律不发。

        赌局有自己的发送通道（不走指令闸门），所以"该群完全静默"这条约定必须
        在这里显式兜一次。策略放在这里而不是 `_wager_send` 里，是因为 _wager_send
        是纯通道、测试会替换它——把策略藏在被替换的方法里等于测不到。
        """
        if self._group_access_blocked(group_id):
            return
        await self._wager_send(group_id, text)

    async def _wager_send(self, group_id: str, text: str) -> None:
        """把播报发到群里（纯发送通道，不含策略）；无 context（单测）时静默跳过。"""
        context = getattr(self, "context", None)
        if context is None or not text:
            return
        umo = self._resolve_umo(group_id)
        if not umo:
            # 从未见过该群的任何消息时拿不到会话串；宁可跳过，也不要发到一个错的地方
            logger.warning(f"[Wager] 群 {group_id} 的会话标识未知，跳过播报")
            return
        try:
            from astrbot.api.message_components import Plain

            from astrbot_plugin_faith_ladder.commands.shared import wrap_message_chain
            await context.send_message(umo, wrap_message_chain([Plain(text=text)]))
        except Exception as e:
            logger.error(f"[Wager] 发送失败: {e}")

    async def _wager_store_open(self, group_id, god, action, seconds) -> Optional[int]:
        """开局落库；没有 db_manager（单测）时返回 None。"""
        db = getattr(self, "db_manager", None)
        if db is None:
            return None
        try:
            return await db.create_wager(group_id, god, action, seconds)
        except Exception as e:
            logger.error(f"[Wager] 开局落库失败: {e}")
            return None

    async def _wager_store_entry(self, wager_id, group_id, player_id, player_name) -> None:
        db = getattr(self, "db_manager", None)
        if db is None or wager_id is None:
            return
        try:
            await db.add_wager_entry(wager_id, group_id, player_id, player_name)
        except Exception as e:
            logger.error(f"[Wager] 入局落库失败: {e}")

    async def _wager_store_close(self, wager_id, winner_id, winner_name, count) -> None:
        db = getattr(self, "db_manager", None)
        if db is None or wager_id is None:
            return
        try:
            await db.finish_wager(wager_id, winner_id, winner_name, count)
        except Exception as e:
            logger.error(f"[Wager] 开奖落库失败: {e}")
