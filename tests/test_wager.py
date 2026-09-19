"""
神明的赌局（沉浸感第三批）。

默认关闭；开启后由调度器每 5 秒 tick 一次：到间隔开局、到时开奖。
这里用可控时钟 + 假发送通道跑完整状态机，并验证默认"只出文案、不动分数"。
"""

import types

import pytest

from astrbot_plugin_faith_ladder import wager_messages as wm
from astrbot_plugin_faith_ladder.commands.config import ConfigMixin
from astrbot_plugin_faith_ladder.commands.gate import GateMixin
from astrbot_plugin_faith_ladder.commands.wager import WAGER_ACTION_LABELS, WagerMixin
from astrbot_plugin_faith_ladder.messages import PERMISSION_DENIED
from astrbot_plugin_faith_ladder.models import VALID_FAITHS

GROUP = "g1"


class _Service:
    def __init__(self):
        self.calls = []

    async def add_score(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return True, "ok"


class _Host(ConfigMixin, GateMixin, WagerMixin):
    def __init__(self, config=None, service=None, db=None, start=1000.0):
        self.config = dict(config or {})
        self.ladder_service = service or _Service()
        self.db_manager = db
        self.sent = []
        self._clock = start
        self._wagers = {}
        self._wager_last = {}
        self.perm_ok = True
        # 会话标识：默认"见过该群消息"，需要模拟未知时把它清掉
        self.umos = {GROUP: f"stub:GroupMessage:{GROUP}"}

    def _wager_now(self):
        return self._clock

    def _get_group_id(self, event):
        return str(event.message_obj.group_id)

    def _resolve_umo(self, group_id):
        return self.umos.get(str(group_id))

    async def _check_perm(self, event):
        return self.perm_ok

    async def _wager_send(self, group_id, text):
        self.sent.append((group_id, text))

    def advance(self, seconds):
        self._clock += seconds


class _Event:
    def __init__(self, sender="555", name="张三", group=GROUP, self_id="1"):
        self._sender = sender
        self._name = name
        self._self_id = self_id
        self.message_obj = types.SimpleNamespace(group_id=group)

    def get_sender_id(self):
        return self._sender

    def get_sender_name(self):
        return self._name

    def get_self_id(self):
        return self._self_id


def _host(**config):
    config.setdefault("wager_enabled", True)
    config.setdefault("wager_groups", [GROUP])
    config.setdefault("wager_duration_seconds", 60)
    config.setdefault("wager_interval_minutes", 30)
    return _Host(config)


class TestWagerAnnounce:
    async def test_disabled_does_nothing(self):
        host = _Host({"wager_enabled": False, "wager_groups": [GROUP]})
        await host._wager_tick()
        assert host.sent == []
        assert host._wager_state() == {}

    async def test_empty_group_list_opens_nothing(self):
        host = _Host({"wager_enabled": True, "wager_groups": []})
        await host._wager_tick()
        assert host.sent == []
        assert host._wager_state() == {}

    async def test_no_wager_open_in_other_groups(self):
        """只会在配置的群里开局；其他群既不发送也不存在状态。"""
        host = _Host({"wager_enabled": True, "wager_groups": ["other"]})
        host.umos["other"] = "stub:GroupMessage:other"
        await host._wager_tick()
        assert [g for g, _ in host.sent] == ["other"]
        assert GROUP not in host._wager_state()


class TestWagerNeedsKnownSession:
    """会话标识未知时不开局。

    否则会出现最让人困惑的现象：开局播报发不出去（尚未见过该群消息、拿不到会话串），
    60 秒后却照常结算——群里只冒出一条"结算"，没有对应的开局提示。
    """

    async def test_announce_skipped_without_umo(self, db_manager):
        host = _host()
        host.db_manager = db_manager
        host.umos.clear()

        await host._wager_tick()

        assert host.sent == []
        assert host._wager_state() == {}
        async with db_manager._db.execute("SELECT COUNT(*) FROM god_wagers") as cursor:
            assert (await cursor.fetchone())[0] == 0, "没开局就不该留下记录"

    async def test_no_orphan_settle_after_skipped_announce(self):
        host = _host()
        host.umos.clear()
        await host._wager_tick()          # 未见过该群消息 → 不开局
        assert host._wager_state() == {}

        host.umos[GROUP] = "stub:GroupMessage:" + GROUP
        host.advance(120)
        await host._wager_tick()          # 现在能开局了：应该先是开局，而不是凭空结算
        state = host._wager_state().get(GROUP)
        assert state is not None, "会话可用后应当先开局"
        assert state["entries"] == {}

        host.advance(59)                  # 开局后 60 秒内不该结算
        await host._wager_tick()
        assert len(host.sent) == 1

    async def test_announce_resumes_once_session_known(self):
        host = _host()
        host.umos.clear()
        await host._wager_tick()
        host.umos[GROUP] = "stub:GroupMessage:" + GROUP
        await host._wager_tick()
        assert len(host.sent) == 1
        assert GROUP in host._wager_state()


class TestWagerIntervalSurvivesRestart:
    """间隔要跨重载生效：内存计时器清零后，用数据库里的开局时间兜底。"""

    async def _seed_wager_row(self, db_manager, started_at: str):
        await db_manager._db.execute(
            "INSERT INTO god_wagers (group_id, god, action, started_at, ends_at) VALUES (?, ?, ?, ?, ?)",
            (GROUP, "欺诈", "speak", started_at, started_at),
        )
        await db_manager.commit()

    async def test_recent_wager_blocks_announce_after_restart(self, db_manager):
        now_str = __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S")
        await self._seed_wager_row(db_manager, now_str)

        # 时钟用真实时间：间隔判定要跟数据库里的 UTC 时间戳相减
        import time as _time

        host = _Host({**_host().config, "wager_interval_minutes": 30}, start=_time.time())
        host.db_manager = db_manager
        await host._wager_tick()

        assert host.sent == [], "库里 30 分钟内有开局记录，重启后不该立刻再开"
        assert host._wager_state() == {}

    async def test_old_wager_allows_announce_after_restart(self, db_manager):
        from datetime import datetime, timedelta, timezone

        old = (datetime.now(timezone.utc) - timedelta(minutes=40)).strftime("%Y-%m-%d %H:%M:%S")
        await self._seed_wager_row(db_manager, old)

        import time as _time

        host = _Host({**_host().config, "wager_interval_minutes": 30}, start=_time.time())
        host.db_manager = db_manager
        await host._wager_tick()

        assert len(host.sent) == 1, "超过间隔后应当开局"
        assert GROUP in host._wager_state()

    async def test_db_timestamp_is_read_in_utc(self, db_manager):
        from datetime import datetime, timezone

        stamp = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        text = "2026-01-01 12:00:00"
        await self._seed_wager_row(db_manager, text)
        assert await db_manager.get_last_wager_started_at(GROUP) == stamp

    async def test_no_records_returns_none(self, db_manager):
        assert await db_manager.get_last_wager_started_at("nobody") is None

    async def test_announce_opens_a_wager(self):
        host = _host()
        await host._wager_tick()

        assert len(host.sent) == 1
        group_id, text = host.sent[0]
        assert group_id == GROUP
        state = host._wager_state()[GROUP]
        assert state["god"] in VALID_FAITHS
        assert state["action"] in WAGER_ACTION_LABELS
        assert state["ends_at"] == 1000.0 + 60
        assert state["entries"] == {}

    async def test_announce_text_comes_from_the_god_pool(self):
        host = _host()
        await host._wager_tick()
        state = host._wager_state()[GROUP]
        pool = wm.WAGER_MESSAGES[state["god"]]["announce"]
        seconds = "60"
        action = WAGER_ACTION_LABELS[state["action"]]
        assert any(line.format(god=state["god"], seconds=seconds, action=action,
                               winner="", reward="") == host.sent[0][1] for line in pool)

    async def test_no_second_announce_before_interval(self):
        host = _host()
        await host._wager_tick()
        host.advance(61)   # 已过时限
        await host._wager_tick()   # 先开奖
        host.advance(60)   # 距上次开局仅 121 秒，未到 30 分钟
        await host._wager_tick()
        assert len(host.sent) == 2, "间隔未到不该再次开局"

    async def test_interval_elapsed_opens_again(self):
        host = _host()
        await host._wager_tick()
        host.advance(61)
        await host._wager_tick()          # 开奖
        host.advance(30 * 60)
        await host._wager_tick()          # 再次开局
        assert len(host.sent) == 3
        assert host._wager_state()[GROUP]["entries"] == {}

    async def test_config_override_pool(self):
        host = _host(wager_messages_announce=["只有这一句 {god} {seconds} {action}"])
        await host._wager_tick()
        state = host._wager_state()[GROUP]
        assert host.sent[0][1] == (
            f"只有这一句 {state['god']} 60 {WAGER_ACTION_LABELS[state['action']]}"
        )


class TestWagerEntry:
    async def _open(self):
        host = _host()
        await host._wager_tick()
        return host, host._wager_state()[GROUP]["action"]

    async def test_matching_action_enters(self):
        host, action = await self._open()
        await host._wager_entry(_Event(), action)
        assert list(host._wager_state()[GROUP]["entries"].values()) == ["张三"]

    async def test_wrong_action_ignored(self):
        host, _ = await self._open()
        other = "pray" if host._wager_state()[GROUP]["action"] == "speak" else "speak"
        await host._wager_entry(_Event(), other)
        assert host._wager_state()[GROUP]["entries"] == {}

    async def test_after_deadline_ignored(self):
        host, action = await self._open()
        host.advance(61)
        await host._wager_entry(_Event(), action)
        assert host._wager_state()[GROUP]["entries"] == {}

    async def test_duplicate_entry_counted_once(self):
        host, action = await self._open()
        await host._wager_entry(_Event(sender="555"), action)
        await host._wager_entry(_Event(sender="555"), action)
        assert len(host._wager_state()[GROUP]["entries"]) == 1

    async def test_bot_itself_is_not_counted(self):
        host, action = await self._open()
        await host._wager_entry(_Event(sender="1", self_id="1"), action)
        assert host._wager_state()[GROUP]["entries"] == {}

    async def test_disabled_wager_ignores_entries(self):
        host = _host()
        await host._wager_tick()
        action = host._wager_state()[GROUP]["action"]
        host.config["wager_enabled"] = False
        await host._wager_entry(_Event(), action)
        assert host._wager_state()[GROUP]["entries"] == {}


class TestWagerSettle:
    async def test_no_entries_uses_none_pool(self):
        host = _host()
        await host._wager_tick()
        state = host._wager_state()[GROUP]
        host.advance(61)
        await host._wager_tick()

        assert len(host.sent) == 2
        pool = wm.WAGER_MESSAGES[state["god"]]["none"]
        assert host.sent[1][1] in [line.format(god=state["god"], winner="", seconds="",
                                               action="", reward="") for line in pool]
        assert GROUP not in host._wager_state()

    async def test_winner_is_one_of_the_participants(self):
        host = _host()
        await host._wager_tick()
        action = host._wager_state()[GROUP]["action"]
        god = host._wager_state()[GROUP]["god"]
        await host._wager_entry(_Event(sender="11", name="甲"), action)
        await host._wager_entry(_Event(sender="22", name="乙"), action)
        host.advance(61)
        await host._wager_tick()

        text = host.sent[1][1]
        assert ("甲" in text) or ("乙" in text), "开奖文案要点名赢家"
        assert god in wm.WAGER_MESSAGES, "神明必须来自 16 信仰"

    async def test_flavor_only_by_default(self):
        """默认不改分：只发文案，并注明不影响实际分数。"""
        host = _host()
        await host._wager_tick()
        action = host._wager_state()[GROUP]["action"]
        await host._wager_entry(_Event(), action)
        host.advance(61)
        await host._wager_tick()

        assert "不影响实际分数" in host.sent[1][1]
        assert host.ladder_service.calls == []

    async def test_scoring_enabled_pays_the_winner(self, monkeypatch):
        monkeypatch.setattr("astrbot_plugin_faith_ladder.commands.wager.WAGER_OUTCOMES", ("win",))
        host = _host(wager_score_enabled=True, wager_reward=5)
        await host._wager_tick()
        action = host._wager_state()[GROUP]["action"]
        await host._wager_entry(_Event(sender="555"), action)
        host.advance(61)
        await host._wager_tick()

        assert len(host.ladder_service.calls) == 1
        args, kwargs = host.ladder_service.calls[0]
        assert args[0] == GROUP and kwargs["pilgrimage_delta"] == 5
        assert "觐见 +5" in host.sent[1][1], "兑现文案必须写明奖励"
        assert "不影响实际分数" not in host.sent[1][1]

    async def test_scoring_enabled_but_losing_outcome_pays_nothing(self, monkeypatch):
        monkeypatch.setattr("astrbot_plugin_faith_ladder.commands.wager.WAGER_OUTCOMES", ("lose",))
        host = _host(wager_score_enabled=True, wager_reward=5)
        await host._wager_tick()
        action = host._wager_state()[GROUP]["action"]
        await host._wager_entry(_Event(), action)
        host.advance(61)
        await host._wager_tick()

        assert host.ladder_service.calls == []
        assert "不影响实际分数" not in host.sent[1][1], "开关打开时不加免责声明"


class TestWagerPersistence:
    async def test_records_are_written(self, db_manager):
        host = _host()
        host.db_manager = db_manager
        await host._wager_tick()
        action = host._wager_state()[GROUP]["action"]
        await host._wager_entry(_Event(sender="555", name="张三"), action)
        host.advance(61)
        await host._wager_tick()

        async with db_manager._db.execute(
            "SELECT god, action, settled, participant_count, winner_name FROM god_wagers"
        ) as cursor:
            rows = await cursor.fetchall()
        assert len(rows) == 1
        god, act, settled, count, winner_name = rows[0]
        assert god in VALID_FAITHS and act == action
        assert settled == 1 and count == 1 and winner_name == "张三"

        async with db_manager._db.execute("SELECT COUNT(*) FROM god_wager_entries") as cursor:
            assert (await cursor.fetchone())[0] == 1

    async def test_duplicate_entries_are_ignored_by_db(self, db_manager):
        wager_id = await db_manager.create_wager(GROUP, "欺诈", "speak", 60)
        assert await db_manager.add_wager_entry(wager_id, GROUP, "u1", "张三") is True
        assert await db_manager.add_wager_entry(wager_id, GROUP, "u1", "张三") is False
        await db_manager.finish_wager(wager_id, "u1", "张三", 1)
        async with db_manager._db.execute(
            "SELECT settled, participant_count FROM god_wagers WHERE id = ?", (wager_id,)
        ) as cursor:
            assert await cursor.fetchone() == (1, 1)

    async def test_missing_db_is_tolerated(self):
        """没有 db_manager（例如单测环境）时整条链路仍然跑通。"""
        host = _host()
        host.db_manager = None
        await host._wager_tick()
        action = host._wager_state()[GROUP]["action"]
        await host._wager_entry(_Event(), action)
        host.advance(61)
        await host._wager_tick()
        assert len(host.sent) == 2


class TestWagerLinePicking:
    def test_builtin_god_pool(self):
        assert wm.pick_wager_line("欺诈", "announce", {}) in wm.WAGER_MESSAGES["欺诈"]["announce"]

    def test_unknown_god_falls_back_to_generic(self):
        pool = wm.GENERIC_WAGER_MESSAGES["win"]
        assert wm.pick_wager_line("不存在的神", "win", {}) in pool

    def test_kind_pool_overrides_god_pool(self):
        config = {"wager_messages_announce": ["通用覆盖 {god}"]}
        assert wm.pick_wager_line("欺诈", "announce", config) == "通用覆盖 {god}"

    def test_per_god_pool_wins(self):
        config = {
            "wager_messages_announce": ["通用覆盖"],
            "wager_messages_announce_欺诈": ["欺诈专属"],
        }
        assert wm.pick_wager_line("欺诈", "announce", config) == "欺诈专属"

    def test_unknown_kind_returns_empty(self):
        assert wm.pick_wager_line("欺诈", "不存在的类型", {}) == ""

    def test_every_win_line_can_show_the_reward(self):
        """兑现文案必须带 {reward}：否则开了改分也看不出给了多少（曾漏配 15 条）。"""
        for god, pools in {**wm.WAGER_MESSAGES}.items():
            for line in pools["win"]:
                assert "{reward}" in line, f"{god} 的兑现文案缺 {{reward}}：{line}"

    def test_every_announce_line_has_placeholders(self):
        for god, pools in wm.WAGER_MESSAGES.items():
            for line in pools["announce"]:
                assert "{seconds}" in line and "{action}" in line, f"{god} 的开局文案缺占位符：{line}"

    def test_every_god_has_every_pool(self):
        for god, pools in wm.WAGER_MESSAGES.items():
            for kind in wm.WAGER_KINDS:
                assert pools.get(kind), f"{god} 缺少 {kind} 文案"


def test_prayer_listener_has_wager_hooks():
    """静态守卫：入局钩子必须挂在'说话'与'献祷词'两条路径上。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "commands" / "prayer.py").read_text(encoding="utf-8")
    assert '_wager_entry(event, "speak")' in src
    assert '_wager_entry(event, "pray")' in src


class TestWagerRespectsGroupAccess:
    """赌局不能绕开群访问控制：被排除的群既不开局、也收不到任何播报。"""

    async def test_blocked_group_gets_no_new_wager(self, db_manager):
        host = _host(group_access_mode="blacklist", group_access_list=[GROUP])
        host.db_manager = db_manager
        await host._wager_tick()
        assert host.sent == []
        assert host._wager_state() == {}
        async with db_manager._db.execute("SELECT COUNT(*) FROM god_wagers") as cursor:
            assert (await cursor.fetchone())[0] == 0, "被排除的群不该留下赌局记录"

    async def test_wager_started_earlier_still_settles_but_silently(self):
        """配置改成排除该群后，进行中的赌局仍要收尾（否则状态永远卡住），但不发消息。"""
        host = _host()
        await host._wager_tick()
        action = host._wager_state()[GROUP]["action"]
        await host._wager_entry(_Event(), action)

        host.config["group_access_mode"] = "blacklist"
        host.config["group_access_list"] = [GROUP]
        host.advance(61)
        await host._wager_tick()

        assert GROUP not in host._wager_state(), "状态必须清掉"
        assert len(host.sent) == 1, "开奖消息不该发到被排除的群"

    async def test_notify_guard_blocks_sends(self):
        host = _host(group_access_mode="whitelist", group_access_list=["other"])
        await host._wager_notify(GROUP, "不该出现")
        assert host.sent == []

    async def test_notify_allows_normal_group(self):
        host = _host()
        await host._wager_notify(GROUP, "正常播报")
        assert host.sent == [(GROUP, "正常播报")]


class TestWagerActionAvailability:
    async def test_action_is_always_speak_without_prayer(self):
        """该群没开祷词（或功能关闭）时，只可能抽到"开口说话"。"""
        host = _host()   # 未配置 prayer_trigger_groups
        for _ in range(30):
            host._wager_state().clear()
            host._wager_last_map().clear()
            await host._wager_tick()
            assert host._wager_state()[GROUP]["action"] == "speak"

    async def test_pray_can_be_drawn_when_prayer_is_available(self, monkeypatch):
        host = _host(prayer_trigger_groups=[GROUP])
        real_choice = wm.random.choice

        def fake_choice(seq):
            if seq == ["speak", "pray"]:
                return "pray"
            return real_choice(seq)

        monkeypatch.setattr("astrbot_plugin_faith_ladder.commands.wager.random.choice", fake_choice)
        await host._wager_tick()
        assert host._wager_state()[GROUP]["action"] == "pray"

    async def test_prayer_feature_switch_off_blocks_pray(self):
        host = _host(prayer_trigger_groups=[GROUP], feature_prayer_enabled=False)
        for _ in range(20):
            host._wager_state().clear()
            host._wager_last_map().clear()
            await host._wager_tick()
            assert host._wager_state()[GROUP]["action"] == "speak"

    async def test_prayer_available_helper(self):
        assert _host()._prayer_available(GROUP) is False
        assert _host(prayer_trigger_groups=[GROUP])._prayer_available(GROUP) is True
        assert _host(prayer_trigger_groups=[GROUP], feature_prayer_enabled=False)._prayer_available(GROUP) is False


class TestManualWagerCommand:
    """手动开局指令：赌局（别名 wager / 神明赌局）。"""

    class _CmdEvent(_Event):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.results = []

        def plain_result(self, text):
            self.results.append(text)
            return text

    async def _run(self, host, event):
        return [r async for r in host._wager_open_impl(event)]

    async def test_manual_open_starts_a_wager(self):
        host = _host(wager_groups=[])   # 不在自动开局名单里也能手动开
        event = self._CmdEvent()
        replies = await self._run(host, event)

        assert len(replies) == 1
        state = host._wager_state()[GROUP]
        assert state["god"] in VALID_FAITHS
        assert state["action"] in WAGER_ACTION_LABELS
        assert host.sent == [], "手动开局的文案由指令本身回复，不再重复广播"

    async def test_manual_open_while_running_reports_remaining(self):
        host = _host()
        await host._wager_tick()
        replies = await self._run(host, self._CmdEvent())
        assert len(replies) == 1
        assert "已有一场赌局" in replies[0]
        assert "还剩" in replies[0]

    async def test_manual_open_needs_master_switch(self):
        host = _host(wager_enabled=False)
        replies = await self._run(host, self._CmdEvent())
        assert "总开关未开启" in replies[0]
        assert host._wager_state() == {}

    async def test_manual_open_needs_permission(self):
        host = _host()
        host.perm_ok = False
        replies = await self._run(host, self._CmdEvent())
        assert replies == [PERMISSION_DENIED["god_only"]]
        assert host._wager_state() == {}

    async def test_manual_open_is_silent_in_blocked_group(self):
        host = _host(group_access_mode="blacklist", group_access_list=[GROUP])
        replies = await self._run(host, self._CmdEvent())
        assert replies == [], "被排除的群里插件应完全静默"
        assert host._wager_state() == {}

    async def test_manual_open_then_entry_counts(self):
        host = _host(wager_groups=[])
        await self._run(host, self._CmdEvent())
        action = host._wager_state()[GROUP]["action"]
        await host._wager_entry(_Event(), action)
        assert list(host._wager_state()[GROUP]["entries"].values()) == ["张三"]

    async def test_manual_open_ignores_interval(self):
        host = _host()
        await host._wager_tick()          # 自动开一场
        action = host._wager_state()[GROUP]["action"]
        await host._wager_entry(_Event(), action)
        host.advance(61)
        await host._wager_tick()          # 开奖
        await self._run(host, self._CmdEvent())   # 立刻手动再开
        assert GROUP in host._wager_state()
