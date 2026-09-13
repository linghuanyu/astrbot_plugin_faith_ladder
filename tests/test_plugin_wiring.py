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


@pytest.fixture(scope="module")
def stubbed_astrbot(tmp_path_factory):
    data_root = tmp_path_factory.mktemp("astrbot_data")
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
