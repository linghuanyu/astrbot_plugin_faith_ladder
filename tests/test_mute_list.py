"""
禁言列表指令（`禁言列表` / `禁言名单`）与它的解析层。

数据来自协议端，而「取不到」与「没人被禁言」长得一模一样：群成员表里没有
shut_up_timestamp 字段时，筛出来的空列表并不代表没人被禁言（多半是机器人没有
群管理权限）。解析层用 None / [] 的区别把两者分开，这些用例把这条约定钉住——
静默回一句「现在没有人被禁言」正是本插件最忌讳的失败方式。
"""

import ast
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_800_000_000


@pytest.fixture(autouse=True)
def _astrbot_stub(stubbed_astrbot):
    """qq_admin_handle 模块级依赖 astrbot，本文件每个用例都要能导入它。"""
    return stubbed_astrbot


class TestNormalizeShutList:
    """协议端扩展接口 get_group_shut_list 的返回值整理。"""

    def _fn(self):
        from astrbot_plugin_faith_ladder.qq_admin_handle import normalize_shut_list

        return normalize_shut_list

    def test_napcat_shape(self):
        entries = self._fn()([{"user_id": 777, "shut_up_timestamp": NOW + 600}], now=NOW)
        assert entries == [{"user_id": "777", "nickname": "", "until": NOW + 600}]

    def test_alternative_field_names(self):
        """有的实现字段是 uin / shutUpTime，也要认。"""
        entries = self._fn()([{"uin": 888, "shutUpTime": NOW + 60}], now=NOW)
        assert entries[0]["user_id"] == "888"
        assert entries[0]["until"] == NOW + 60

    def test_millisecond_timestamps_are_converted(self):
        """毫秒时间戳（13 位）要折算成秒，否则会显示成"剩余 5 万年"。"""
        entries = self._fn()([{"user_id": 1, "shut_up_timestamp": (NOW + 600) * 1000}], now=NOW)
        assert entries[0]["until"] == NOW + 600

    def test_expired_entries_are_dropped(self):
        """有的实现会把全群成员连 shut_up_timestamp=0 一起返回，不能照单全收。"""
        entries = self._fn()(
            [
                {"user_id": 1, "shut_up_timestamp": 0},
                {"user_id": 2, "shut_up_timestamp": NOW - 1},
                {"user_id": 3, "shut_up_timestamp": NOW + 1},
            ],
            now=NOW,
        )
        assert [e["user_id"] for e in entries] == ["3"]

    def test_empty_list_is_a_valid_result(self):
        """空列表是「确实没人被禁言」，与认不出来的 None 必须区分。"""
        assert self._fn()([], now=NOW) == []

    def test_list_wrapped_in_dict(self):
        entries = self._fn()({"list": [{"user_id": 1, "shut_up_timestamp": NOW + 5}]}, now=NOW)
        assert [e["user_id"] for e in entries] == ["1"]

    def test_unrecognizable_returns_none(self):
        """认不出来就交给调用方回退，不能假装"没人被禁言"。"""
        for raw in ("oops", None, 42, [{"user_id": 1}], [{"shut_up_timestamp": NOW + 5}],
                    [{"user_id": 1, "shut_up_timestamp": "abc"}], ["x"], [{"uin": "", "shutUpTime": NOW + 5}]):
            assert self._fn()(raw, now=NOW) is None, raw


class TestMutedFromMembers:
    """群成员表路径（OneBot11 标准接口，自带群名片）。"""

    def _fn(self):
        from astrbot_plugin_faith_ladder.qq_admin_handle import muted_from_members

        return muted_from_members

    def test_picks_only_muted_and_prefers_card(self):
        entries = self._fn()(
            [
                {"user_id": 777, "card": "张三", "nickname": "小张", "shut_up_timestamp": NOW + 60},
                {"user_id": 888, "card": "", "nickname": "李四", "shut_up_timestamp": 0},
            ],
            now=NOW,
        )
        assert entries == [{"user_id": "777", "nickname": "张三", "until": NOW + 60}]

    def test_field_present_but_nobody_muted(self):
        entries = self._fn()([{"user_id": 1, "shut_up_timestamp": 0}], now=NOW)
        assert entries == []

    def test_missing_field_returns_none_not_empty(self):
        """整份数据里没有禁言时间字段 → None（协议端没提供），不是"没人被禁言"。"""
        assert self._fn()([{"user_id": 1, "nickname": "张三"}], now=NOW) is None
        assert self._fn()([], now=NOW) is None


class TestFormatRemaining:
    def test_two_levels_only(self):
        from astrbot_plugin_faith_ladder.message_formatter import format_remaining

        assert format_remaining(0) == "0 秒"
        assert format_remaining(30) == "30 秒"
        assert format_remaining(2700) == "45 分钟"
        assert format_remaining(3600) == "1 小时"
        assert format_remaining(3661) == "1 小时 1 分钟"
        assert format_remaining(86400 + 3 * 3600) == "1 天 3 小时"
        assert format_remaining(86400) == "1 天"
        assert format_remaining(-5) == "0 秒"


class TestFormatMuteList:
    def test_empty(self):
        from astrbot_plugin_faith_ladder.message_formatter import format_mute_list

        assert "现在没有人被禁言" in format_mute_list([])

    def test_sorted_by_remaining_and_qq_only_when_anonymous(self):
        from astrbot_plugin_faith_ladder.message_formatter import format_mute_list

        text = format_mute_list(
            [
                {"user_id": "1", "nickname": "张三", "until": NOW + 60},
                {"user_id": "2", "nickname": "", "until": NOW + 7200},
            ],
            now=NOW,
        )
        lines = [line for line in text.splitlines() if line.strip().startswith(("1.", "2."))]
        assert "2 — 剩余 2 小时" in lines[0], "剩得久的在前；无昵称时只显示 QQ"
        assert "张三(1)" in lines[1]
        assert "共 2 人" in text

    def test_over_limit_only_counts_the_rest(self):
        from astrbot_plugin_faith_ladder.message_formatter import (
            MUTE_LIST_DISPLAY_LIMIT,
            format_mute_list,
        )

        entries = [
            {"user_id": str(i), "nickname": "", "until": NOW + 60}
            for i in range(MUTE_LIST_DISPLAY_LIMIT + 3)
        ]
        text = format_mute_list(entries, now=NOW)
        assert f"另有 3 人未列出" in text
        assert f"共 {MUTE_LIST_DISPLAY_LIMIT + 3} 人" in text


class _FakeBot:
    def __init__(self, members=None, members_error=None, shut=None, shut_error=None):
        self._members = members
        self._members_error = members_error
        self._shut = shut
        self._shut_error = shut_error
        self.member_list_calls = 0

    async def get_group_member_list(self, group_id=None):
        self.member_list_calls += 1
        if self._members_error:
            raise self._members_error
        return self._members

    async def call_action(self, action, **params):
        assert action == "get_group_shut_list"
        if self._shut_error:
            raise self._shut_error
        return self._shut


class _FakeEvent:
    def __init__(self, bot):
        self.bot = bot
        self.stopped = False
        self.message_str = "禁言列表"

    def get_messages(self):
        return []

    def get_self_id(self):
        return "1"

    def get_sender_id(self):
        return "555"

    def get_group_id(self):
        return "1"

    def plain_result(self, text):
        return text

    def stop_event(self):
        self.stopped = True


def _handler(perm=True, gate_fn=None, names=None):
    from astrbot_plugin_faith_ladder.qq_admin_handle import QQAdminHandler

    async def check_perm(uid):
        return perm

    def is_admin(event):
        return False

    handler = QQAdminHandler(check_perm_fn=check_perm, check_admin_fn=is_admin, gate_fn=gate_fn)

    async def member_info(event, uid):
        return dict((names or {}).get(uid, {}))

    handler._get_member_info = member_info
    return handler


class TestMuteListCommand:
    async def test_lists_members_from_member_list(self):
        now = int(time.time())
        bot = _FakeBot(
            members=[
                {"user_id": 777, "card": "张三", "shut_up_timestamp": now + 3600},
                {"user_id": 888, "nickname": "李四", "shut_up_timestamp": 0},
            ]
        )
        event = _FakeEvent(bot)

        replies = [r async for r in _handler().handle_mute_list(event)]

        assert len(replies) == 1
        assert "张三(777)" in replies[0] and "共 1 人" in replies[0]
        assert "李四" not in replies[0], "没被禁言的人不该上榜"
        assert event.stopped is True

    async def test_nobody_muted(self):
        now = int(time.time())
        bot = _FakeBot(members=[{"user_id": 1, "shut_up_timestamp": 0}])

        replies = [r async for r in _handler().handle_mute_list(_FakeEvent(bot))]

        assert "现在没有人被禁言" in replies[0]

    async def test_zeroed_member_table_is_cross_checked(self):
        """成员表说"没人被禁言"时也要问一次扩展接口。

        有的协议端在机器人不是管理员时会把 shut_up_timestamp 一律给 0，只信成员表
        这一份就会把"读不到"显示成"没人被禁言"。
        """
        now = int(time.time())
        bot = _FakeBot(
            members=[{"user_id": 1, "shut_up_timestamp": 0}],
            shut=[{"user_id": 999, "shut_up_timestamp": now + 120}],
        )

        replies = [r async for r in _handler(names={"999": {"nickname": "被禁的人"}}).handle_mute_list(_FakeEvent(bot))]

        assert "被禁的人(999)" in replies[0]
        assert "没有人被禁言" not in replies[0]

    async def test_zeroed_member_table_without_extension_still_reports_nobody(self):
        """扩展接口不存在时只能信成员表（这里钉住行为，不做无根据的猜测）。"""
        bot = _FakeBot(members=[{"user_id": 1, "shut_up_timestamp": 0}], shut_error=RuntimeError("no such action"))

        replies = [r async for r in _handler().handle_mute_list(_FakeEvent(bot))]

        assert "现在没有人被禁言" in replies[0]

    async def test_missing_field_falls_back_and_fills_names(self):
        """群成员表没有禁言时间字段时用扩展接口，缺的昵称再补一次群成员信息。"""
        now = int(time.time())
        bot = _FakeBot(
            members=[{"user_id": 777, "card": "张三"}],  # 字段整个缺失
            shut=[{"user_id": 777, "shut_up_timestamp": now + 600}],  # 扩展接口不带昵称
        )

        replies = [r async for r in _handler(names={"777": {"card": "小七"}}).handle_mute_list(_FakeEvent(bot))]

        assert "小七(777)" in replies[0]

    async def test_unavailable_reports_error_instead_of_nobody(self):
        """两条路都拿不到数据时如实报错——绝不能静默说"没有人被禁言"。"""
        bot = _FakeBot(members=[{"user_id": 777, "card": "张三"}], shut_error=RuntimeError("no such action"))

        replies = [r async for r in _handler().handle_mute_list(_FakeEvent(bot))]

        assert len(replies) == 1
        assert "取不到禁言信息" in replies[0]
        assert "没有人被禁言" not in replies[0]

    async def test_member_list_failure_falls_back_to_shut_list(self):
        now = int(time.time())
        bot = _FakeBot(
            members_error=RuntimeError("权限不足"),
            shut=[{"user_id": 777, "shut_up_timestamp": now + 60}],
        )

        replies = [r async for r in _handler().handle_mute_list(_FakeEvent(bot))]

        assert "777" in replies[0] and "共 1 人" in replies[0]

    async def test_both_unavailable(self):
        bot = _FakeBot(members_error=RuntimeError("权限不足"), shut_error=RuntimeError("no such action"))

        replies = [r async for r in _handler().handle_mute_list(_FakeEvent(bot))]

        assert "取不到群成员列表" in replies[0]

    async def test_requires_god_permission(self):
        bot = _FakeBot(members=[{"user_id": 1, "shut_up_timestamp": 0}])

        replies = [r async for r in _handler(perm=False).handle_mute_list(_FakeEvent(bot))]

        assert replies == ["此等权柄，唯诸神方可执掌。"]
        assert bot.member_list_calls == 0, "没权限就不该去取数据"

    async def test_gate_silent_block(self):
        async def silent_block(event, action):
            return True, None

        bot = _FakeBot(members=[])
        event = _FakeEvent(bot)

        replies = [r async for r in _handler(gate_fn=silent_block).handle_mute_list(event)]

        assert replies == []
        assert event.stopped is False, "插件未启用的群里不该抢事件"

    async def test_gate_action_name(self):
        seen = []

        async def spy(event, action):
            seen.append(action)
            return False, None

        _ = [r async for r in _handler(gate_fn=spy).handle_mute_list(_FakeEvent(_FakeBot(members=[])))]
        assert seen == ["qq_admin"]


class TestMuteListRegistration:
    def _command_decorators(self):
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                func = dec.func
                if isinstance(func, ast.Attribute) and func.attr == "command" and dec.args:
                    names = [a.value for a in dec.args if isinstance(a, ast.Constant)]
                    aliases = [
                        elt.value
                        for kw in dec.keywords if kw.arg == "alias" and isinstance(kw.value, ast.Set)
                        for elt in kw.value.elts if isinstance(elt, ast.Constant)
                    ]
                    found.append((node.name, names, aliases))
        return found

    def test_registered_in_main_with_alias(self):
        """装饰器只留在 main.py（mixin 里注册会重复扫到），且带「禁言名单」别名。"""
        entries = [e for e in self._command_decorators() if "禁言列表" in e[1]]
        assert entries, "main.py 里找不到「禁言列表」的注册"
        _, _, aliases = entries[0]
        assert "禁言名单" in aliases

    def test_does_not_collide_with_ban_command(self):
        """「禁言列表」不会被「禁言」抢走。

        上游 CommandFilter 只认「整串等于完整指令」或「完整指令 + 空格开头」
        （astrbot/core/star/filter/command.py 的 filter()），所以更长的指令名安全。
        """
        message = "禁言列表"
        assert not (message == "禁言" or message.startswith("禁言 "))
        assert message.startswith("禁言列表")

    def test_help_mentions_command(self):
        from astrbot_plugin_faith_ladder.message_formatter import format_help

        assert "禁言列表" in format_help({})
