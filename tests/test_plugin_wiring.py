"""
插件启动冒烟测试：真实执行 FaithLadderPlugin 的 __init__ / initialize / terminate。

这是本轮补上的关键防线。此前 365 个测试**没有一个导入 main.py**（它依赖 astrbot），
于是 initialize() 里的属性错误（`self.db_manager.purge_old_score_history` 被误删）无人发现，
线上插件因此静默降级了数个版本：调度器从未启动 → 备份、积分历史清理、过期状态清理、
赠送超时清理全部停摆，白名单成员变动监听也没注册。

做法：把最小 astrbot 桩注入 sys.modules，使 main.py 可在无 AstrBot 的环境导入，
然后真实跑一遍启动流程（会建库、执行全部迁移、启动并停止调度器）。
"""

import asyncio
import sys
import types
from pathlib import Path

import pytest


def _module(name: str, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def _make_filter_module():
    """最小的 filter 桩：装饰器只记录元数据并原样返回函数。"""

    mod = types.ModuleType("astrbot.api.event.filter")

    def _record(kind):
        def deco(*args, **kwargs):
            def wrapper(func):
                handlers = list(getattr(func, "__handlers__", []))
                handlers.append({"kind": kind, "args": args, "kwargs": kwargs})
                func.__handlers__ = handlers
                return func
            return wrapper
        return deco

    for kind in ("command", "regex", "event_message_type", "command_group",
                 "llm_tool", "on_astrbot_loaded", "permission_type"):
        setattr(mod, kind, _record(kind))

    class PermissionType:
        ADMIN = "admin"

    mod.PermissionType = PermissionType
    sys.modules[mod.__name__] = mod
    return mod


def _install_astrbot_stub(data_root: Path):
    """注入最小 astrbot 桩。返回被覆盖的 sys.modules 原值，供测试结束后还原。"""
    saved = {
        name: module for name, module in list(sys.modules.items())
        if name == "astrbot" or name.startswith("astrbot.")
    }

    class AstrMessageEvent:
        pass

    class Star:
        def __init__(self, context=None):
            self.context = context
            self.config = {}

    class Context:
        def __init__(self):
            self.event_handlers = []

        def register_event_handler(self, fn):
            self.event_handlers.append(fn)

        def send_message(self, umo, chain):  # pragma: no cover - 冒烟测试不会触发
            raise NotImplementedError

    class Plain:
        def __init__(self, text=""):
            self.text = text

    class At:
        def __init__(self, qq=None):
            self.qq = qq

    class Reply:
        def __init__(self, id=None):
            self.id = id

    class Node:
        def __init__(self, user_id=None, nickname=None, content=None):
            self.user_id, self.nickname, self.content = user_id, nickname, content

    class Image:
        @staticmethod
        def fromBytes(b):  # noqa: N802 - 与框架 API 同名
            return Image()

    class MessageChain:
        """v4 的发送载体：框架内部会访问 `.chain`，直接传 list 会报
        `'list' object has no attribute 'chain'`（赌局播报就栽在这里）。"""

        def __init__(self, chain=None):
            self.chain = list(chain or [])

    _module("astrbot")
    _module("astrbot.api", logger=__import__("logging").getLogger("astrbot-stub"))
    _module("astrbot.api.event", filter=_make_filter_module(), AstrMessageEvent=AstrMessageEvent)
    _module("astrbot.api.star", Context=Context, Star=Star,
            register=lambda *a, **k: (lambda cls: cls))
    _module("astrbot.api.message_components", Plain=Plain, Image=Image)
    _module("astrbot.core")
    _module("astrbot.core.message")
    _module("astrbot.core.message.components", Plain=Plain, At=At, Reply=Reply, Node=Node)
    _module("astrbot.core.message.message_event_result", MessageChain=MessageChain)
    _module("astrbot.core.utils")
    _module("astrbot.core.utils.astrbot_path", get_astrbot_data_path=lambda: data_root)
    _module("astrbot.core.platform")
    _module("astrbot.core.platform.sources")
    _module("astrbot.core.platform.sources.aiocqhttp")
    _module("astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event",
            AiocqhttpMessageEvent=AstrMessageEvent)
    return saved


@pytest.fixture
def stubbed_astrbot(tmp_path):
    # 每个用例独立的数据目录：module 作用域会让数据库状态在用例间泄漏
    data_root = tmp_path / "astrbot_data"
    saved = _install_astrbot_stub(data_root)
    try:
        yield data_root
    finally:
        for name in list(sys.modules):
            if name == "astrbot" or name.startswith("astrbot."):
                del sys.modules[name]
        sys.modules.update(saved)


class TestPluginStartup:
    """启动流程必须端到端跑通。"""

    async def test_initialize_starts_scheduler_and_migrates(self, stubbed_astrbot):
        import astrbot_plugin_faith_ladder.main as m

        context = m.Context()
        plugin = m.FaithLadderPlugin(context, {})

        # initialize 必须放在 try 内：它一旦失败（例如某个回调属性不存在），
        # finally 里的 terminate 才不会被执行，数据库连接与 aiosqlite 工作线程
        # 会一直挂着，pytest 将无法退出——闸门变成"超时"而不是"失败"。
        try:
            await plugin.initialize()

            # 关键回归点：这些回调此前会因 purge_old_score_history 被误删而抛 AttributeError，
            # 异常被框架吞掉 → 调度器从未构建 → 备份等全部停摆
            assert plugin._scheduler is not None, "调度器未构建"
            assert callable(plugin._scheduler._purge_score_history)
            assert callable(plugin._scheduler._purge_daily_tables)
            assert callable(plugin._scheduler._purge_expired_statuses)
            assert callable(plugin._scheduler._backup_db)
            assert callable(plugin._scheduler._cleanup_expired_gifts)

            # 两个定时任务确实被创建
            assert plugin._scheduler._backup_task is not None
            assert plugin._scheduler._gift_cleanup_task is not None

            # 数据库已建好且迁移跑完
            assert plugin.db_manager._initialized is True
            assert await plugin.db_manager._table_exists("players")
            assert await plugin.db_manager._table_exists("player_items")

            # 白名单成员变动监听已注册（它在崩溃点之后，此前同样没注册上）
            assert context.event_handlers, "群成员变动监听未注册"
        finally:
            await plugin.terminate()

    async def test_terminate_is_safe_without_start(self, stubbed_astrbot):
        """terminate 在未 start 的情况下也不应抛异常。"""
        import astrbot_plugin_faith_ladder.main as m

        plugin = m.FaithLadderPlugin(m.Context(), {})
        await plugin.terminate()

    async def test_daily_maintenance_callbacks_are_wired(self, stubbed_astrbot, tmp_path):
        """每日任务实际调用积分历史清理与每日表清理（两者都不应抛异常）。"""
        import astrbot_plugin_faith_ladder.main as m

        context = m.Context()
        plugin = m.FaithLadderPlugin(context, {})
        try:
            await plugin.initialize()
            purged = await plugin.db_manager.purge_old_score_history(90)
            assert purged >= 0
            purged = await plugin.db_manager.purge_daily_tables(90)
            assert purged >= 0
        finally:
            await plugin.terminate()

    async def test_wager_tick_is_wired(self, stubbed_astrbot):
        """赌局的定时入口必须接上调度器（默认关闭，但循环要存在）。"""
        import astrbot_plugin_faith_ladder.main as m

        plugin = m.FaithLadderPlugin(m.Context(), {})
        try:
            await plugin.initialize()
            assert plugin._scheduler._wager_tick is not None
            assert plugin._scheduler._wager_task is not None, "wager 循环应随调度器启动"
            # 默认关闭时 tick 不应做任何事（含不访问数据库）
            assert plugin._cfg("wager_enabled") is False
            await plugin._wager_tick()
            assert plugin._wager_state() == {}
        finally:
            await plugin.terminate()

    async def test_wish_tick_is_wired(self, stubbed_astrbot):
        """祈愿试炼的定时入口与每日清理必须接上调度器。

        默认 `wish_groups` 为空（哪个群都不启用），但**循环与回调要存在**：漏传的话
        提醒与超时解散会静默失效——功能看起来是好的，只是队伍永远不超时、永远不催人。
        """
        import astrbot_plugin_faith_ladder.main as m

        plugin = m.FaithLadderPlugin(m.Context(), {})
        try:
            await plugin.initialize()
            assert plugin._scheduler._wish_tick is not None
            assert plugin._scheduler._wish_task is not None, "wish 循环应随调度器启动"
            assert callable(plugin._scheduler._purge_old_wish_teams)

            # 默认没有任何群启用 → tick 不该产生播报；顺带证明它端到端跑得通
            assert plugin._cfg("wish_groups") == []
            await plugin._wish_tick()

            # 历史队伍清理在空库上返回 0（同时证明那段 SQL 是合法的）
            assert await plugin.db_manager.purge_old_wish_teams(7) == 0
        finally:
            await plugin.terminate()

    async def test_wish_service_is_wired(self, stubbed_astrbot):
        """祈愿服务必须挂在插件上，且拿到的是同一份配置读取入口。"""
        import astrbot_plugin_faith_ladder.main as m

        plugin = m.FaithLadderPlugin(m.Context(), {"wish_groups": ["12345"]})
        try:
            await plugin.initialize()
            assert plugin.wish_service is not None
            assert plugin.wish_service.groups() == ["12345"]
        finally:
            await plugin.terminate()

class TestQQAdminCallbacks:
    """QQAdminHandler 注入的回调必须是可 await 的（或至少被兼容处理）。

    `_is_plugin_admin` 是同步方法，而 `_check_permission` 原先直接 await 它的返回值，
    于是"只在 admin_ids、不在白名单"的账号一用群管指令就
    TypeError: object bool can't be used in 'await' expression。
    """

    class _FakeEvent:
        def get_sender_id(self):
            return "999"

        def stop_event(self):
            pass

    async def test_sync_admin_callback_is_supported(self, stubbed_astrbot):
        from astrbot_plugin_faith_ladder.qq_admin_handle import QQAdminHandler

        async def deny_perm(uid):
            return False

        def allow_admin(event):  # 同步回调，正是插件注入的形态
            return True

        handler = QQAdminHandler(check_perm_fn=deny_perm, check_admin_fn=allow_admin)
        assert await handler._check_permission(self._FakeEvent()) is True

    async def test_whitelist_hit_short_circuits(self, stubbed_astrbot):
        from astrbot_plugin_faith_ladder.qq_admin_handle import QQAdminHandler

        async def allow_perm(uid):
            return True

        def deny_admin(event):
            return False

        handler = QQAdminHandler(check_perm_fn=allow_perm, check_admin_fn=deny_admin)
        assert await handler._check_permission(self._FakeEvent()) is True

    async def test_neither_source_denies(self, stubbed_astrbot):
        from astrbot_plugin_faith_ladder.qq_admin_handle import QQAdminHandler

        async def deny_perm(uid):
            return False

        def deny_admin(event):
            return False

        handler = QQAdminHandler(check_perm_fn=deny_perm, check_admin_fn=deny_admin)
        assert await handler._check_permission(self._FakeEvent()) is False

    async def test_async_admin_callback_also_supported(self, stubbed_astrbot):
        """异步回调同样要能工作（兼容两种写法）。"""
        from astrbot_plugin_faith_ladder.qq_admin_handle import QQAdminHandler

        async def deny_perm(uid):
            return False

        async def allow_admin(event):
            return True

        handler = QQAdminHandler(check_perm_fn=deny_perm, check_admin_fn=allow_admin)
        assert await handler._check_permission(self._FakeEvent()) is True

    async def test_default_faith_callback_is_awaitable(self, stubbed_astrbot):
        """不传 get_faith_fn 时默认实现必须可 await（否则成功文案会崩）。"""
        from astrbot_plugin_faith_ladder.qq_admin_handle import QQAdminHandler

        async def deny_perm(uid):
            return False

        def deny_admin(event):
            return False

        handler = QQAdminHandler(check_perm_fn=deny_perm, check_admin_fn=deny_admin)
        assert await handler._get_faith("999") is None

class TestRecallHandlerGuards:
    """撤回指令的边界：空消息链、历史消息缺 sender。"""

    class _FakeEvent:
        message_str = "撤回"
        bot = None  # handle_recall 会先取 event.bot

        def __init__(self, chain=()):
            self._chain = list(chain)
            self.stopped = False

        def get_sender_id(self):
            return "999"

        def get_group_id(self):
            return "1"

        def get_messages(self):
            return self._chain

        def get_self_id(self):
            return "1"

        def plain_result(self, text):
            return text

        def stop_event(self):
            self.stopped = True

    @staticmethod
    def _handler():
        from astrbot_plugin_faith_ladder.qq_admin_handle import QQAdminHandler

        async def allow(uid):
            return True

        def allow_admin(event):
            return True

        return QQAdminHandler(check_perm_fn=allow, check_admin_fn=allow_admin)

    async def test_empty_chain_does_not_raise(self, stubbed_astrbot):
        """空消息链此前会 chain[0] 抛 IndexError，且 stop_event 不会执行。"""
        event = self._FakeEvent(chain=[])
        results = [r async for r in self._handler().handle_recall(event)]
        assert results == ["没有可撤回的消息。"]
        assert event.stopped is True

    async def test_reply_target_rejected_when_bot_cannot_delete(self, stubbed_astrbot):
        """撤回引用的消息失败时给出提示（event.bot 缺失 → 静默异常路径）。"""
        from astrbot_plugin_faith_ladder.qq_admin_handle import Reply

        event = self._FakeEvent(chain=[Reply(id="1")])
        event.bot = None  # 取 client.delete_msg 会失败，走 except 分支
        results = [r async for r in self._handler().handle_recall(event)]
        assert results == ["消息已过期或不存在"]
        assert event.stopped is True


class TestBackupGuards:
    """备份的保留期与"当天已跑过"判断。"""

    async def test_retention_zero_keeps_fresh_backup(self, stubbed_astrbot, tmp_path):
        """保留天数为 0 时，截止时间落在"现在"，会把刚生成的备份也删掉。"""
        from astrbot_plugin_faith_ladder.scheduler_service import SchedulerService

        calls = []

        async def fake_backup(dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text("x", encoding="utf-8")
            calls.append(dest)

        sched = SchedulerService(
            data_dir=tmp_path,
            get_config=lambda: {},
            backup_db=fake_backup,
        )
        await sched._do_backup({"backup_retention_days": 0})

        assert len(calls) == 1
        assert calls[0].exists(), "保留天数被钳到 1，刚生成的备份不应被自己删掉"

    async def test_latest_backup_date_detects_today(self, stubbed_astrbot, tmp_path):
        """重启后不应重跑当天的备份：靠已有文件名判断。"""
        from astrbot_plugin_faith_ladder.db_manager import BEIJING_TZ
        from astrbot_plugin_faith_ladder.scheduler_service import SchedulerService
        from datetime import datetime

        sched = SchedulerService(data_dir=tmp_path, get_config=lambda: {})
        assert sched._latest_backup_date() is None

        today = datetime.now(BEIJING_TZ).strftime("%Y%m%d")
        backups = tmp_path / "backups"
        backups.mkdir(parents=True, exist_ok=True)
        (backups / f"ladder_backup_{today}_120000.db").write_text("x", encoding="utf-8")
        (backups / f"ladder_backup_20200101_000000.db").write_text("x", encoding="utf-8")
        assert sched._latest_backup_date() == today

    async def test_same_second_backups_do_not_collide(self, stubbed_astrbot, tmp_path):
        from astrbot_plugin_faith_ladder.db_manager import BEIJING_TZ
        from astrbot_plugin_faith_ladder.scheduler_service import SchedulerService
        from datetime import datetime

        created = []

        async def fake_backup(dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text("x", encoding="utf-8")
            created.append(dest.name)

        sched = SchedulerService(data_dir=tmp_path, get_config=lambda: {}, backup_db=fake_backup)
        await sched._do_backup({})
        await sched._do_backup({})
        assert len(set(created)) == 2, f"同一秒的两次备份不应同名: {created}"

    async def test_backup_failure_is_reported_to_caller(self, stubbed_astrbot, tmp_path):
        """备份失败要能被调用方看见：此前异常被吞掉，返回值也没有。"""
        from astrbot_plugin_faith_ladder.scheduler_service import SchedulerService

        async def failing_backup(dest):
            raise RuntimeError("disk full")

        sched = SchedulerService(data_dir=tmp_path, get_config=lambda: {}, backup_db=failing_backup)
        assert await sched._do_backup({}) is False

        async def ok_backup(dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text("x", encoding="utf-8")

        sched_ok = SchedulerService(data_dir=tmp_path, get_config=lambda: {}, backup_db=ok_backup)
        assert await sched_ok._do_backup({}) is True

    async def test_failed_backup_retries_next_cycle(
        self, stubbed_astrbot, tmp_path, monkeypatch
    ):
        """备份失败不能把当天标记为已完成。

        否则「自动备份开着」会安静地变成「今天没有备份」，而且一整天不再重试——
        这正是备份失败被吞掉 + 无条件记日期组合出来的后果。
        """
        from astrbot_plugin_faith_ladder import scheduler_service as mod

        calls = []

        async def failing_backup(dest):
            calls.append(dest)
            raise RuntimeError("disk full")

        sched = mod.SchedulerService(
            data_dir=tmp_path,
            get_config=lambda: {"auto_backup_enabled": True},
            backup_db=failing_backup,
        )
        sched._running = True
        iterations = {"n": 0}
        real_sleep = asyncio.sleep  # 下面会替换 asyncio.sleep，先留一份原函数

        async def fast_sleep(_seconds):
            # 把 10 分钟的睡眠换成立刻返回，并跑够几轮就停：循环是死循环，
            # 只有「备份失败 → 下个周期再来一次」才会让 calls 增长
            iterations["n"] += 1
            if iterations["n"] >= 3:
                sched._running = False
            await real_sleep(0)

        monkeypatch.setattr(mod.asyncio, "sleep", fast_sleep)
        await sched._backup_loop()

        assert len(calls) >= 2, "备份失败后应当在下一个周期重试，而不是当天不再备份"


class TestCheckPlayerPermissionGate:
    """「检测玩家」的权限已收归诸神/管理员。

    它的身份解析会回退到群名片（弱身份），并可能据此把发送者 QQ 绑到名片对应的
    玩家记录上；开放给所有人时，任何人把名片改成他人名字发一次即可抢占对方绑定，
    随后用「赠送道具」取走其库存。限制调用者后该路径只由受信用户触发。
    """

    class _FakeBot:
        def __init__(self, nickname, card):
            self._info = {"nickname": nickname, "card": card}

        async def get_group_member_info(self, group_id=None, user_id=None):
            return dict(self._info)

    class _FakeEvent:
        def __init__(self, *, card, nickname="任意昵称", sender="555"):
            import types

            self.message_obj = types.SimpleNamespace(group_id="1")
            self.bot = TestCheckPlayerPermissionGate._FakeBot(nickname, card)
            self._sender = sender
            self.stopped = False

        def get_sender_id(self):
            return self._sender

        def plain_result(self, text):
            return text

        def stop_event(self):
            self.stopped = True

    @staticmethod
    async def _make_plugin(stubbed_astrbot, *, admin_ids=()):
        import astrbot_plugin_faith_ladder.main as m

        plugin = m.FaithLadderPlugin(m.Context(), {"admin_ids": list(admin_ids)})
        await plugin.db_manager.initialize()
        await plugin.db_manager.upsert_player("1", "name:张三", "张三")
        await plugin.db_manager.commit()
        return plugin

    async def test_non_god_is_denied(self, stubbed_astrbot):
        """普通人调用 → 权限拒绝，且不产生任何绑定。"""
        plugin = await self._make_plugin(stubbed_astrbot, admin_ids=[])
        try:
            replies = [r async for r in plugin._check_player_impl(self._FakeEvent(card="张三"))]
            assert any("唯诸神" in r for r in replies)
            player = await plugin.db_manager.get_player_by_name("1", "张三")
            assert player.qq_id is None, "被拒的调用不应产生绑定"
        finally:
            await plugin.terminate()

    async def test_god_can_check_and_bind(self, stubbed_astrbot):
        """诸神调用 → 可检测，且名片能对上数据库玩家名即可完成绑定。"""
        plugin = await self._make_plugin(stubbed_astrbot, admin_ids=["555"])
        try:
            replies = [r async for r in plugin._check_player_impl(self._FakeEvent(card="张三"))]
            player = await plugin.db_manager.get_player_by_name("1", "张三")
            assert player.qq_id == "555"
            assert any("已自动绑定" in r for r in replies)
        finally:
            await plugin.terminate()

    async def test_card_without_matching_player(self, stubbed_astrbot):
        """名片对不上任何玩家 → 提示无法识别，不产生绑定。"""
        plugin = await self._make_plugin(stubbed_astrbot, admin_ids=["555"])
        try:
            replies = [r async for r in plugin._check_player_impl(self._FakeEvent(card="查无此人"))]
            assert any("无法识别" in r for r in replies)
            player = await plugin.db_manager.get_player_by_name("1", "张三")
            assert player.qq_id is None
        finally:
            await plugin.terminate()

    async def test_already_bound_reports_status(self, stubbed_astrbot):
        plugin = await self._make_plugin(stubbed_astrbot, admin_ids=["555"])
        try:
            await plugin.db_manager.set_player_qq("1", "name:张三", "555")
            await plugin.db_manager.commit()
            replies = [r async for r in plugin._check_player_impl(self._FakeEvent(card="张三"))]
            assert any("已绑定 555" in r for r in replies)
        finally:
            await plugin.terminate()


class TestBanDurationParsing:
    """禁言时长取**第一个**数字词。

    此前内层 break 只跳出内层循环（外层继续扫后续 Plain 段），
    「禁言 60 @某人 我记得 30 秒」会被解析成 30 秒。
    """

    class _Plain:
        """本地段类型：不依赖 astrbot 桩的类身份。

        stub fixture 是函数作用域，每个用例都会重建 astrbot 模块（新的 Plain 类），
        而 qq_admin_handle 已被缓存、持有旧的 Plain —— 直接 isinstance 会判假、
        数字段被整体跳过，测试就会"因为默认值恰好等于期望值"而虚假通过。
        """

        def __init__(self, text):
            self.text = text

    class _At:
        def __init__(self, qq):
            self.qq = qq

    class _Bot:
        def __init__(self):
            self.bans = []

        async def set_group_ban(self, group_id=None, user_id=None, duration=None):
            self.bans.append((user_id, duration))

    class _FakeEvent:
        def __init__(self, segments, bot):
            self._segments = segments
            self.bot = bot
            self.stopped = False
            self.message_str = ""

        def get_messages(self):
            return self._segments

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

    @staticmethod
    def _handler():
        from astrbot_plugin_faith_ladder.qq_admin_handle import QQAdminHandler

        async def allow(uid):
            return True

        def allow_admin(event):
            return True

        handler = QQAdminHandler(check_perm_fn=allow, check_admin_fn=allow_admin)

        async def member_info(event, uid):
            return {"role": "member", "nickname": f"用户{uid}"}

        handler._get_member_info = member_info
        return handler

    async def _run(self, monkeypatch, segments):
        from astrbot_plugin_faith_ladder import qq_admin_handle

        # 段类型与 @ 目标解析都替换成本地实现，避免受 stub 类身份影响
        monkeypatch.setattr(qq_admin_handle, "Plain", self._Plain)
        monkeypatch.setattr(qq_admin_handle, "get_ats", lambda event: ["777"])
        bot = self._Bot()
        event = self._FakeEvent(segments, bot)
        _ = [r async for r in self._handler().handle_ban(event)]
        assert event.stopped is True
        return bot

    async def test_first_number_wins_over_later_digits(self, stubbed_astrbot, monkeypatch):
        bot = await self._run(
            monkeypatch,
            [self._Plain("禁言 60 "), self._At(qq="777"), self._Plain("我记得 30 秒")],
        )
        assert bot.bans == [(777, 60)]  # user_id 被 int() 转换

    async def test_number_found_when_first_segment_has_none(self, stubbed_astrbot, monkeypatch):
        bot = await self._run(
            monkeypatch, [self._Plain("禁言 "), self._At(qq="777"), self._Plain("30 秒")]
        )
        assert bot.bans == [(777, 30)]

    async def test_default_is_60_when_no_digits(self, stubbed_astrbot, monkeypatch):
        bot = await self._run(
            monkeypatch, [self._Plain("禁言 "), self._At(qq="777"), self._Plain("拜托了")]
        )
        assert bot.bans == [(777, 60)]


class TestQQAdminGate:
    """群管指令的入口闸门（群访问控制 / 功能开关）。

    这 8 条指令不走 mixin，而是 main.py 直接调用 QQAdminHandler.handle_*，
    所以闸门靠注入的回调 `gate_fn` 接入，这里验证三种结果。
    """

    class _Bot:
        def __init__(self):
            self.bans = []

        async def set_group_ban(self, group_id=None, user_id=None, duration=None):
            self.bans.append((user_id, duration))

    class _FakeEvent:
        def __init__(self, bot):
            self.bot = bot
            self.stopped = False
            self.message_str = ""
            self._segments = []

        def get_messages(self):
            return self._segments

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

    @staticmethod
    def _handler(monkeypatch, gate_fn):
        from astrbot_plugin_faith_ladder import qq_admin_handle
        from astrbot_plugin_faith_ladder.qq_admin_handle import QQAdminHandler

        async def allow(uid):
            return True

        def allow_admin(event):
            return True

        monkeypatch.setattr(qq_admin_handle, "get_ats", lambda event: ["777"])
        handler = QQAdminHandler(
            check_perm_fn=allow, check_admin_fn=allow_admin, gate_fn=gate_fn
        )

        async def member_info(event, uid):
            return {"role": "member", "nickname": f"用户{uid}"}

        handler._get_member_info = member_info
        return handler

    async def test_silent_block_does_not_act_or_stop(self, stubbed_astrbot, monkeypatch):
        async def silent_block(event, action):
            return True, None

        bot = self._Bot()
        event = self._FakeEvent(bot)
        replies = [r async for r in self._handler(monkeypatch, silent_block).handle_ban(event)]
        assert replies == []
        assert bot.bans == [], "被静默拦截时不应执行禁言"
        assert event.stopped is False, "插件未启用的群里不该抢事件（让其他插件/AI 正常处理）"

    async def test_block_with_message_replies_and_stops(self, stubbed_astrbot, monkeypatch):
        async def feature_off(event, action):
            return True, "「群管」功能已被管理员关闭"

        bot = self._Bot()
        event = self._FakeEvent(bot)
        replies = [r async for r in self._handler(monkeypatch, feature_off).handle_ban(event)]
        assert replies == ["「群管」功能已被管理员关闭"]
        assert bot.bans == []
        assert event.stopped is True

    async def test_without_gate_callback_nothing_is_blocked(self, stubbed_astrbot, monkeypatch):
        """不注入 gate_fn 时保持原行为（QQAdminHandler 仍可独立复用/测试）。"""
        bot = self._Bot()
        event = self._FakeEvent(bot)
        _ = [r async for r in self._handler(monkeypatch, None).handle_ban(event)]
        assert bot.bans == [(777, 60)]

    async def test_gate_receives_plugin_action_name(self, stubbed_astrbot, monkeypatch):
        seen = []

        async def spy(event, action):
            seen.append(action)
            return False, None

        event = self._FakeEvent(self._Bot())
        _ = [r async for r in self._handler(monkeypatch, spy).handle_ban(event)]
        assert seen == ["qq_admin"]


def test_message_chain_is_wrapped_not_a_raw_list():
    """静态守卫：给框架发消息必须包成 MessageChain。

    直接传 list 在 v4 上抛 `'list' object has no attribute 'chain'`，
    消息被静默丢弃——赌局开奖就是这样丢了整条播报。
    用 AST 只看真实调用（文档字符串里出现示例写法不算违规）。
    """
    import ast

    root = Path(__file__).resolve().parent.parent
    offenders = []
    for path in [root / "main.py"] + sorted((root / "commands").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "send_message"):
                continue
            if len(node.args) >= 2 and isinstance(node.args[1], ast.List):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], "这些调用直接给 send_message 传了 list：" + "；".join(offenders)


class TestMessageChainWrapping:
    def test_wrap_returns_message_chain(self, stubbed_astrbot):
        from astrbot.api.message_components import Plain
        from astrbot_plugin_faith_ladder.commands.shared import wrap_message_chain

        plain = Plain(text="x")
        chain = wrap_message_chain([plain])
        assert hasattr(chain, "chain"), "必须是 MessageChain 对象"
        assert chain.chain == [plain]

    def test_wrap_falls_back_to_list_without_framework(self, stubbed_astrbot, monkeypatch):
        """老版本或导入失败时退回 list（v3 直接收 list），不能因此报错。"""
        import builtins
        from astrbot_plugin_faith_ladder.commands.shared import wrap_message_chain

        real_import = builtins.__import__

        def deny(name, *args, **kwargs):
            if "message_event_result" in name or name.endswith("message_components"):
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", deny)
        assert wrap_message_chain([1, 2]) == [1, 2]


class TestGroupUmoResolution:
    """定时任务发消息需要完整会话串（AstrBot v4 形如 xiaoyu:GroupMessage:<群号>）。

    此前两处（赌局播报、赠送超时通知）手工拼 `group:<群号>`，只有两段；v4.28 的
    send_message 会报「不合法的 session 字符串」，消息被丢掉、群里毫无动静。
    """

    class _Event:
        def __init__(self, group_id="111", umo="xiaoyu:GroupMessage:111"):
            import types

            self.message_obj = types.SimpleNamespace(group_id=group_id)
            if umo is not None:
                self.unified_msg_origin = umo

    @staticmethod
    async def _plugin(stubbed_astrbot):
        import astrbot_plugin_faith_ladder.main as m

        plugin = m.FaithLadderPlugin(m.Context(), {})
        return plugin

    async def test_group_event_records_umo(self, stubbed_astrbot):
        plugin = await self._plugin(stubbed_astrbot)
        try:
            assert plugin._get_group_id(self._Event()) == "111"
            assert plugin._resolve_umo("111") == "xiaoyu:GroupMessage:111"
        finally:
            await plugin.terminate()

    async def test_unknown_group_derives_from_known_platform(self, stubbed_astrbot):
        """没见过该群消息时，用已知平台前缀拼一个（同一机器人通常只有一个平台）。"""
        plugin = await self._plugin(stubbed_astrbot)
        try:
            plugin._remember_umo("111", self._Event())
            assert plugin._resolve_umo("999") == "xiaoyu:GroupMessage:999"
        finally:
            await plugin.terminate()

    async def test_no_known_platform_returns_none(self, stubbed_astrbot):
        plugin = await self._plugin(stubbed_astrbot)
        try:
            assert plugin._resolve_umo("999") is None
        finally:
            await plugin.terminate()

    async def test_event_without_umo_is_tolerated(self, stubbed_astrbot):
        plugin = await self._plugin(stubbed_astrbot)
        try:
            assert plugin._get_group_id(self._Event(umo=None)) == "111"
            assert plugin._resolve_umo("111") is None
        finally:
            await plugin.terminate()

    async def test_wager_send_uses_full_session(self, stubbed_astrbot):
        sent = []

        async def capture(umo, chain):
            sent.append((umo, chain))

        plugin = await self._plugin(stubbed_astrbot)
        try:
            plugin.context.send_message = capture
            plugin._remember_umo("111", self._Event())
            await plugin._wager_send("111", "播报")
            assert [umo for umo, _ in sent] == ["xiaoyu:GroupMessage:111"]
        finally:
            await plugin.terminate()

    async def test_wager_send_skips_when_session_unknown(self, stubbed_astrbot):
        sent = []

        async def capture(umo, chain):
            sent.append(umo)

        plugin = await self._plugin(stubbed_astrbot)
        try:
            plugin.context.send_message = capture
            await plugin._wager_send("999", "播报")
            assert sent == [], "会话未知时宁可跳过，也不要发到错的地方"
        finally:
            await plugin.terminate()


def test_no_legacy_session_string_in_production():
    """静态守卫：生产代码不得再手工拼 `group:<群号>`（v4 会直接报错）。"""
    root = Path(__file__).resolve().parent.parent
    offenders = []
    for path in [root / "main.py"] + sorted((root / "commands").glob("*.py")):
        src = path.read_text(encoding="utf-8")
        if 'f"group:{' in src or "f'group:{" in src:
            offenders.append(path.name)
    assert offenders == [], f"仍有旧式会话串: {offenders}"
