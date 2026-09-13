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

    _module("astrbot")
    _module("astrbot.api", logger=__import__("logging").getLogger("astrbot-stub"))
    _module("astrbot.api.event", filter=_make_filter_module(), AstrMessageEvent=AstrMessageEvent)
    _module("astrbot.api.star", Context=Context, Star=Star,
            register=lambda *a, **k: (lambda cls: cls))
    _module("astrbot.api.message_components", Plain=Plain, Image=Image)
    _module("astrbot.core")
    _module("astrbot.core.message")
    _module("astrbot.core.message.components", Plain=Plain, At=At, Reply=Reply, Node=Node)
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
