"""
祈愿试炼指令层（mixin）测试。

用真 `DatabaseManager` + 真 `WishService` 跑通「指令 → 服务 → 库」的完整链路，
只把框架相关的部分替身掉（事件、发送通道、权限、冷却）。

时钟必须钉死：名额按「发起日 + 状态天数」的星期算，不固定住就可能在关闭日
（周三/周日）跑测试，`祈愿组队` 会直接被拒——那种失败与代码对错无关。
"""

import types
from datetime import datetime, timedelta

import pytest
import pytest_asyncio

from astrbot_plugin_faith_ladder.commands.config import ConfigMixin
from astrbot_plugin_faith_ladder.commands.gate import GateMixin
from astrbot_plugin_faith_ladder.commands.wish import (
    UNBOUND_MSG,
    WishCommandsMixin,
    parse_capacity,
    split_tail_int,
)
from astrbot_plugin_faith_ladder.db_manager import BEIJING_TZ, WISH_DISBANDED, DatabaseManager
from astrbot_plugin_faith_ladder.messages import PERMISSION_DENIED, STATUS_BLOCKED_MSG
from astrbot_plugin_faith_ladder.text_utils import strip_mentions
from astrbot_plugin_faith_ladder.wish_service import WishService

GROUP = "g1"
TEAM_NAME = "09月28日祈愿试炼"  # 基准日 2026-09-25（周五）+ 3 天 = 09-28（周一）


class _Player:
    def __init__(self, player_id="name:甲", player_name="甲", group_id=GROUP):
        self.player_id = player_id
        self.player_name = player_name
        self.group_id = group_id


class _Cooldown:
    """可切换的冷却器：默认放行。"""

    def __init__(self):
        self.allowed = True
        self.started = []

    def check_cooldown(self, key, seconds):
        return self.allowed

    def get_remaining(self, key, seconds):
        return float(seconds)

    def set_cooldown(self, key):
        self.started.append(key)


class _Host(ConfigMixin, GateMixin, WishCommandsMixin):
    def __init__(self, db, service, config=None):
        # 直接持有传入的 dict（不复制）：测试要靠改同一个 dict 来模拟 WebUI 改配置，
        # 复制一份会让改动只被服务看到、宿主看不到
        self.config = config if config is not None else {}
        self.db_manager = db
        self.wish_service = service
        self.cooldown_manager = _Cooldown()
        self.sent = []
        self.perm_ok = True
        self.player = _Player()
        self.umos = {GROUP: f"stub:GroupMessage:{GROUP}"}

    def _get_group_id(self, event):
        return str(event.message_obj.group_id)

    def _get_args(self, event, cmd_name):
        """与 main.py 的同名helper一致（它留在插件类上，不在 mixin 里）。"""
        text = event.message_str.strip()
        if not text.startswith(cmd_name):
            return ""
        return strip_mentions(text[len(cmd_name):])

    def _resolve_umo(self, group_id):
        return self.umos.get(str(group_id))

    async def _check_perm(self, event):
        return self.perm_ok

    async def _resolve_self_player(self, event):
        return self.player

    async def _resolve_self_player_lenient(self, event):
        return self.player

    async def _wish_send(self, group_id, text):
        self.sent.append((group_id, text))


class _Event:
    def __init__(self, text="", group=GROUP, sender="555"):
        self.message_str = text
        self._sender = sender
        self.message_obj = types.SimpleNamespace(group_id=group)
        self.results = []

    def get_sender_id(self):
        return self._sender

    def get_sender_name(self):
        return "甲"

    def get_self_id(self):
        return "1"

    def plain_result(self, text):
        self.results.append(text)
        return text


class _FakeContext:
    def __init__(self):
        self.sent = []

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))


class _RealSendHost(_Host):
    """不替换发送通道的宿主：用来验证 `_wish_send` 自己的门槛。

    `_Host` 把 `_wish_send` 换成了纯记录（测试播报内容与策略），但那也绕过了
    「context 缺失 / 会话串未知就跳过」——那两条只能由真通道来测。
    """

    async def _wish_send(self, group_id, text):
        return await WishCommandsMixin._wish_send(self, group_id, text)


async def _collect(agen):
    return [item async for item in agen]


@pytest_asyncio.fixture
async def ctx(temp_data_dir):
    db = DatabaseManager(temp_data_dir)
    await db.initialize()

    config = {"wish_groups": [GROUP]}
    service = WishService(db, lambda: config)
    # 2026-09-25 是周五：名额日期 = 09-28（周一，允许 1 支）
    friday = datetime(2026, 9, 25, 12, 0, tzinfo=BEIJING_TZ)
    assert friday.weekday() == 4
    service.now = lambda: friday

    for who in ["甲", "乙", "丙", "丁", "戊"]:
        await db.upsert_player(GROUP, f"name:{who}", who)

    host = _Host(db, service, config)
    yield host, db, service, config
    await db.close()


async def _create_team(host, capacity=2, who=None) -> str:
    """用宿主跑一次 `祈愿组队`。

    默认 2 人（schema 默认是 6）：这些用例要的多是「一加入就满员发车」，
    容量留大反而测不到发车分支。
    who 用来换发起者：同一个宿主连开第二次会撞上 already_in_team（他还在自己那支队里）。
    """
    if who is not None:
        host.player = _Player(f"name:{who}", who)
    text = "祈愿组队" if capacity is None else f"祈愿组队 {capacity}"
    event = _Event(text)
    await _collect(host._wish_create_impl(event))
    return event.results[0]


class TestParseHelpers:
    def test_capacity_forms(self):
        assert parse_capacity("") == (None, None)
        assert parse_capacity(" 4 ") == (4, None)
        assert parse_capacity("12") == (12, None)

    def test_capacity_rejects_names(self):
        _, error = parse_capacity("我的队")
        assert error and "不能自定义" in error
        _, error = parse_capacity("4 5")
        assert error is not None

    def test_split_tail_int(self):
        assert split_tail_int("深渊队 3") == ("深渊队", [3])
        assert split_tail_int("我 的 队 3 2", count=2) == ("我 的 队", [3, 2])
        assert split_tail_int("深渊队") == ("深渊队", [])
        assert split_tail_int("队 名 abc") == ("队 名 abc", [])


class TestPlayerCommands:
    async def test_hall_shows_quota(self, ctx):
        host, _, _, _ = ctx
        event = _Event("祈愿")
        await _collect(host._wish_impl(event))

        assert "本群招募中" in event.results[0]
        assert "名额：1 支，已用 0 支" in event.results[0]

    async def test_create_reports_and_broadcasts(self, ctx):
        host, db, _, _ = ctx
        event = _Event("祈愿组队 3")
        await _collect(host._wish_create_impl(event))

        assert TEAM_NAME in event.results[0]
        assert len(host.sent) == 1, "发起没有向群里播报"
        assert "甲" in host.sent[0][1]
        assert await db.get_wish_team_by_name(GROUP, TEAM_NAME) is not None

    async def test_create_rejects_team_name_argument(self, ctx):
        host, db, _, _ = ctx
        event = _Event("祈愿组队 我的队")
        await _collect(host._wish_create_impl(event))

        assert "不能自定义" in event.results[0]
        assert await db.get_open_wish_teams(GROUP) == []

    async def test_create_requires_binding(self, ctx):
        host, _, _, _ = ctx
        host.player = None

        event = _Event("祈愿组队")
        await _collect(host._wish_create_impl(event))
        assert event.results[0] == UNBOUND_MSG

    async def test_group_must_be_listed(self, ctx):
        host, _, _, config = ctx
        config["wish_groups"] = []

        for impl, text in [
            (host._wish_impl, "祈愿"),
            (host._wish_create_impl, "祈愿组队"),
            (host._wish_join_impl, "祈愿加入"),
            (host._wish_leave_impl, "祈愿退出"),
            (host._wish_mine_impl, "祈愿我的"),
            (host._wish_random_impl, "祈愿随机"),
        ]:
            event = _Event(text)
            await _collect(impl(event))
            assert "未在本群开启" in event.results[0], text

    async def test_feature_toggle_blocks(self, ctx):
        host, _, _, config = ctx
        config["feature_wish_enabled"] = False

        event = _Event("祈愿")
        await _collect(host._wish_impl(event))
        assert "已被管理员关闭" in event.results[0]

    async def test_group_access_silent(self, ctx):
        host, _, _, config = ctx
        config["group_access_mode"] = "blacklist"
        config["group_access_list"] = [GROUP]

        event = _Event("祈愿")
        await _collect(host._wish_impl(event))
        assert event.results == [], "被排除的群必须完全静默"
        assert host.sent == []

    async def test_status_block_can_stop_wish(self, ctx):
        """阻断标签里有「祈愿」时（阻断=祈愿）该指令被拦下。"""
        host, db, _, _ = ctx
        await db.add_status(GROUP, "name:甲", "沉默", 3, block_actions="wish")
        await db.commit()

        event = _Event("祈愿组队")
        await _collect(host._wish_create_impl(event))
        assert event.results[0] == STATUS_BLOCKED_MSG.format(status="沉默", action="祈愿")

    async def test_join_without_name(self, ctx):
        host, db, _, _ = ctx
        await _create_team(host, capacity=3)  # 甲（队长）在队里，所以加入者要换个人
        host.player = _Player("name:乙", "乙")

        event = _Event("祈愿加入")
        await _collect(host._wish_join_impl(event))
        assert "已加入" in event.results[0]
        names = [m["player_name"] for m in (await db.get_wish_team_by_name(GROUP, TEAM_NAME))["members"]]
        assert names == ["甲", "乙"]

    async def test_join_full_departs_with_broadcasts(self, ctx):
        host, db, _, _ = ctx
        await _create_team(host)  # 甲，容量 2
        host.sent.clear()  # 发起那一条不算
        host.player = _Player("name:乙", "乙")

        event = _Event("祈愿加入")
        await _collect(host._wish_join_impl(event))
        assert "满员发车" in event.results[0]
        assert len(host.sent) == 1
        assert [s["status_name"] for s in await db.get_player_statuses(GROUP, "name:甲")] == [TEAM_NAME]

    async def test_join_unknown_team(self, ctx):
        host, _, _, _ = ctx
        event = _Event("祈愿加入 不存在的队")
        await _collect(host._wish_join_impl(event))
        assert "没有叫" in event.results[0]

    async def test_leave_leader_disbands(self, ctx):
        host, db, _, _ = ctx
        await _create_team(host)

        event = _Event("祈愿退出")
        await _collect(host._wish_leave_impl(event))
        assert "解散" in event.results[0]
        assert (await db.get_wish_team_by_name(GROUP, TEAM_NAME))["status"] == WISH_DISBANDED

    async def test_mine(self, ctx):
        host, _, _, _ = ctx
        await _create_team(host)

        event = _Event("祈愿我的")
        await _collect(host._wish_mine_impl(event))
        assert "队长" in event.results[0]

    async def test_random_joins_and_starts_cooldown(self, ctx):
        host, db, service, _ = ctx
        # 另一个人开的队伍（甲不在其中），容量留出空位以免直接发车
        await db.upsert_player(GROUP, "name:乙", "乙")
        await service.create(GROUP, "name:乙", "乙", capacity=4)
        await service.join(GROUP, "name:丙", "丙")

        event = _Event("祈愿随机")
        await _collect(host._wish_random_impl(event))
        assert "已加入" in event.results[0]
        assert host.cooldown_manager.started == ["555:wish:random"]

    async def test_cooldown_blocks_join(self, ctx):
        host, _, _, _ = ctx
        await _create_team(host)
        host.cooldown_manager.allowed = False

        event = _Event("祈愿加入")
        await _collect(host._wish_join_impl(event))
        assert "冷却中" in event.results[0]


class TestAdminCommand:
    async def test_requires_god_permission(self, ctx):
        host, _, _, _ = ctx
        host.perm_ok = False

        event = _Event("祈愿管理 列表")
        await _collect(host._wish_admin_impl(event))
        assert event.results[0] == PERMISSION_DENIED["god_only"]

    async def test_usage_when_no_action(self, ctx):
        host, _, _, _ = ctx
        event = _Event("祈愿管理")
        await _collect(host._wish_admin_impl(event))
        assert "用法：祈愿管理" in event.results[0]

    async def test_unknown_action(self, ctx):
        host, _, _, _ = ctx
        event = _Event("祈愿管理 乱写")
        await _collect(host._wish_admin_impl(event))
        assert "未识别的操作" in event.results[0]

    async def test_list_excludes_disbanded_and_shows_disbanded(self, ctx):
        host, _, service, _ = ctx
        await _create_team(host)
        await service.admin_rename(GROUP, TEAM_NAME, "深渊队")
        await service.admin_disband(GROUP, "深渊队")
        await _create_team(host, who="乙")  # 换个人开：每人每天只能开一次

        listed = _Event("祈愿管理 列表")
        await _collect(host._wish_admin_impl(listed))
        assert TEAM_NAME in listed.results[0]
        assert "深渊队" not in listed.results[0]

        gone = _Event("祈愿管理 已解散")
        await _collect(host._wish_admin_impl(gone))
        assert "深渊队" in gone.results[0]

    async def test_roster_with_scores(self, ctx):
        host, _, service, _ = ctx
        await _create_team(host)

        event = _Event(f"祈愿管理 名单 {TEAM_NAME} 16 2")
        await _collect(host._wish_admin_impl(event))
        assert "【玩家：甲】" in event.results[0]
        assert "【登神之路+16】" in event.results[0]
        assert "【觐见之梯+2】" in event.results[0]

    async def test_roster_plain_without_scores(self, ctx):
        host, _, _, _ = ctx
        await _create_team(host)

        event = _Event(f"祈愿管理 名单 {TEAM_NAME}")
        await _collect(host._wish_admin_impl(event))
        assert "【玩家：甲】" not in event.results[0]
        assert "批量录入" in event.results[0]

    async def test_swap_by_player_names(self, ctx):
        """用户约定的形态：换人 玩家1 玩家2（不带队名，由玩家1 定位队伍）。"""
        host, db, service, _ = ctx
        await _create_team(host)  # 甲（队长，2 人队）
        await service.join(GROUP, "name:乙", "乙")  # 发车
        host.sent.clear()

        event = _Event("祈愿管理 换人 乙 丙")
        await _collect(host._wish_admin_impl(event))
        assert "换成 丙" in event.results[0]
        assert len(host.sent) == 1, "换人没有向群里播报"
        assert await db.get_player_statuses(GROUP, "name:乙") == []
        assert [s["status_name"] for s in await db.get_player_statuses(GROUP, "name:丙")] == [TEAM_NAME]

    async def test_swap_requires_two_names(self, ctx):
        host, _, _, _ = ctx
        event = _Event("祈愿管理 换人 乙")
        await _collect(host._wish_admin_impl(event))
        assert "用法：祈愿管理 换人" in event.results[0]

    async def test_rename_with_arrow(self, ctx):
        host, db, _, _ = ctx
        await _create_team(host)

        event = _Event("祈愿管理 重命名 09月28日祈愿试炼 → 深 渊 队")
        await _collect(host._wish_admin_impl(event))
        assert "深 渊 队" in event.results[0]
        assert await db.get_wish_team_by_name(GROUP, "深 渊 队") is not None

    async def test_remove_and_fill(self, ctx):
        host, _, service, _ = ctx
        await _create_team(host)
        await service.join(GROUP, "name:乙", "乙")

        removed = _Event(f"祈愿管理 移出 {TEAM_NAME} 甲")
        await _collect(host._wish_admin_impl(removed))
        assert "移出" in removed.results[0]

        filled = _Event(f"祈愿管理 补位 {TEAM_NAME} 丁")
        await _collect(host._wish_admin_impl(filled))
        assert "补位" in filled.results[0]

    async def test_extend(self, ctx):
        host, _, service, _ = ctx
        await _create_team(host)
        await service.join(GROUP, "name:乙", "乙")

        event = _Event(f"祈愿管理 延期 {TEAM_NAME} 2")
        await _collect(host._wish_admin_impl(event))
        assert "延期 2 天" in event.results[0]

        bad = _Event(f"祈愿管理 延期 {TEAM_NAME}")
        await _collect(host._wish_admin_impl(bad))
        assert "用法：祈愿管理 延期" in bad.results[0]

    async def test_stats_command(self, ctx):
        host, _, _, _ = ctx
        await _create_team(host)

        event = _Event("祈愿管理 统计")
        await _collect(host._wish_admin_impl(event))
        assert "发起 1 次" in event.results[0]

        bad = _Event("祈愿管理 统计 七天")
        await _collect(host._wish_admin_impl(bad))
        assert "用法：祈愿管理 统计" in bad.results[0]

    async def test_bonus_command(self, ctx):
        """祈愿管理 加开：默认 +1、可给数值、写错给用法。"""
        host, _, service, _ = ctx
        event = _Event("祈愿管理 加开 2")
        await _collect(host._wish_admin_impl(event))
        assert "加开 2 场" in event.results[0]
        assert await service.slot_bonus(GROUP, service.slot_date_of()) == 2

        bad = _Event("祈愿管理 加开 两场")
        await _collect(host._wish_admin_impl(bad))
        assert "用法：祈愿管理 加开" in bad.results[0]

    async def test_clear_needs_confirm(self, ctx):
        host, db, _, _ = ctx
        await _create_team(host)

        ask = _Event("祈愿管理 清空")
        await _collect(host._wish_admin_impl(ask))
        assert "确认请发送" in ask.results[0]
        assert await db.get_open_wish_teams(GROUP), "未确认就清了"

        do = _Event("祈愿管理 清空 确认")
        await _collect(host._wish_admin_impl(do))
        assert "已释放" in do.results[0]
        assert await db.get_open_wish_teams(GROUP) == []

    async def test_admin_works_outside_wish_groups(self, ctx):
        """功能关掉/群从名单里移除后，诸神仍要能进来收拾残局。"""
        host, _, _, config = ctx
        await _create_team(host)
        config["wish_groups"] = []
        config["feature_wish_enabled"] = False

        event = _Event("祈愿管理 列表")
        await _collect(host._wish_admin_impl(event))
        assert "本群队伍" in event.results[0]


class TestSchedulerAndEvents:
    async def test_tick_broadcasts_reminder(self, ctx):
        host, db, _, _ = ctx
        await _create_team(host)
        host.sent.clear()  # 发起那一条不算，只看 tick 发出来的
        team = await db.get_wish_team_by_name(GROUP, TEAM_NAME)
        await db._db.execute(
            "UPDATE wish_teams SET last_reminded_at = '2000-01-01 00:00:00' WHERE id = ?",
            (team["team_id"],),
        )
        await db._db.commit()

        await host._wish_tick()
        assert len(host.sent) == 1
        assert TEAM_NAME in host.sent[0][1]

    async def test_tick_respects_group_access(self, ctx):
        host, db, _, config = ctx
        await _create_team(host)
        team = await db.get_wish_team_by_name(GROUP, TEAM_NAME)
        await db._db.execute(
            "UPDATE wish_teams SET last_reminded_at = '2000-01-01 00:00:00' WHERE id = ?",
            (team["team_id"],),
        )
        await db._db.commit()

        config["group_access_mode"] = "blacklist"
        config["group_access_list"] = [GROUP]
        host.sent.clear()
        await host._wish_tick()
        assert host.sent == [], "被排除的群不该收到任何播报"

    async def test_real_send_skips_unknown_umo(self, ctx, stubbed_astrbot):
        """真发送通道的门槛：context 缺失或会话串未知时跳过，不发到别处去。"""
        host, db, service, config = ctx
        real = _RealSendHost(db, service, config)
        real.context = _FakeContext()
        real.umos = {}  # 从未见过该群的消息

        await real._wish_broadcast(GROUP, ["测试播报"])
        assert real.context.sent == [], "会话串未知时不该发送"

        real.umos = {GROUP: f"stub:GroupMessage:{GROUP}"}
        await real._wish_broadcast(GROUP, ["测试播报"])
        assert len(real.context.sent) == 1
        umo, chain = real.context.sent[0]
        assert umo == f"stub:GroupMessage:{GROUP}"
        # 必须是 MessageChain 对象：直接传 list 会被 v4 静默丢弃（赌局踩过这个坑）
        assert hasattr(chain, "chain"), f"播报没有包成 MessageChain：{chain!r}"
