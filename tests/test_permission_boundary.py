"""
权限边界测试：超管 = admin_ids，群主/群管理员 = 诸神级。

锁住的是「群主/群管理员不再自动获得管理类操作」这一变更。此前
`_is_plugin_admin` 把 config.admin_ids 与 QQ 群角色混成一个判定，并被当成
**管理员闸门**直接用在白名单管理与天梯榜管理上——于是任何一个群的群主都能
全局增删白名单、清空本群数据、重置全员积分。
"""

import pytest

from astrbot_plugin_faith_ladder.commands.config import ConfigMixin
from astrbot_plugin_faith_ladder.commands.gate import GateMixin
from astrbot_plugin_faith_ladder.messages import PERMISSION_DENIED
from astrbot_plugin_faith_ladder.permission_service import PermissionService


# --- 缓存层：不依赖 astrbot ---

class _FakeDB:
    """只实现 PermissionService 会用到的方法；hook 用于在查询"途中"插入副作用。"""

    def __init__(self, result=True):
        self.result = result
        self.hook = None

    async def is_whitelisted(self, user_id):
        if self.hook:
            self.hook()
        return self.result


class TestPermissionCache:
    async def test_invalidation_during_query_is_not_cached(self):
        """失效发生在 DB 查询途中时，那个"失效前的结论"不能进缓存。

        此前 check_score_permission 先读缓存、await DB、再无条件回写：
        期间白名单被移除并 invalidate 了缓存，旧结论仍会带着新时间戳写回，
        被移除的人可以继续通过最长 5 分钟。
        """
        db = _FakeDB(True)
        svc = PermissionService(db)
        db.hook = lambda: svc.invalidate_cache("u1")  # 查询途中失效

        assert await svc.check_score_permission("u1") is True
        assert "u1" not in svc._permission_cache, "查询途中失效过，结果不应进缓存"

        db.hook = None
        db.result = False
        assert await svc.check_score_permission("u1") is False

    async def test_cache_size_is_bounded(self):
        """缓存条数必须有上限：从未重复出现的 user 不会被过期清理碰到。"""
        svc = PermissionService(_FakeDB(True))
        svc.MAX_CACHE_SIZE = 8
        for i in range(50):
            await svc.check_score_permission(f"u{i}")
        assert len(svc._permission_cache) <= 8

    async def test_per_id_invalidation_keeps_other_entries(self, db_manager):
        """按 id 失效只影响目标，其他人不必重查。"""
        svc = PermissionService(db_manager)
        await svc.check_score_permission("keepme")
        await svc.check_score_permission("dropme")
        svc.invalidate_cache("dropme")
        assert "keepme" in svc._permission_cache
        assert "dropme" not in svc._permission_cache


# --- 存储层：白名单表只承认 user 条目 ---

class TestWhitelistStorage:
    async def test_non_user_entries_are_not_authorised_or_listed(self, db_manager):
        """非 user 条目（待审用 'pending'）既不算授权，也不出现在诸神列表里。"""
        await db_manager.add_to_whitelist("user", "u1", "admin")
        await db_manager.add_to_whitelist("pending", "u2", "auto")

        assert await db_manager.is_whitelisted("u2") is False
        rows = await db_manager.get_whitelist_with_faith()
        assert [r["entry_id"] for r in rows] == ["u1"]


# --- 群访问名单：脏条目不该被当成群号 ---

class _GateHost(ConfigMixin, GateMixin):
    def __init__(self, config):
        self.config = config


class TestGroupAccessListHygiene:
    def test_none_entries_are_not_group_ids(self):
        """str(None) == "None" 曾会被当成一个真实群号留在名单里。"""
        host = _GateHost({"group_access_mode": "blacklist",
                          "group_access_list": [None, "", "200"]})
        assert host._group_access_blocked("None") is False
        assert host._group_access_blocked("200") is True


# --- 判定本身：跑真实插件实例（需要 astrbot 桩）---

class _Sender:
    def __init__(self, role):
        self.role = role


class _MessageObj:
    def __init__(self, role, group_id):
        self.sender = _Sender(role)
        self.group_id = group_id


class _Event:
    """main.py 判定所需的最小事件：发送者 / 群角色 / 群号 / 命令文本。"""

    def __init__(self, sender_id="10001", role="member", group_id="100", text=""):
        self._sender_id = sender_id
        self.message_obj = _MessageObj(role, group_id)
        self.message_str = text

    def get_sender_id(self):
        return self._sender_id

    def plain_result(self, text):
        return text


@pytest.fixture
async def plugin(stubbed_astrbot):
    import astrbot_plugin_faith_ladder.main as m

    p = m.FaithLadderPlugin(m.Context(), {"admin_ids": ["999"]})
    await p.initialize()
    try:
        yield p
    finally:
        await p.terminate()


class TestRoleBoundary:
    """群角色只能到诸神级；管理类操作只认 admin_ids。"""

    def test_super_admin_is_config_only(self, plugin):
        assert plugin._is_super_admin(_Event(sender_id="999", role="member")) is True
        # 关键回归点：群主/群管理员不是超管
        assert plugin._is_super_admin(_Event(sender_id="555", role="owner")) is False
        assert plugin._is_super_admin(_Event(sender_id="555", role="admin")) is False

    def test_group_moderator_detection(self, plugin):
        assert plugin._is_group_moderator(_Event(role="owner")) is True
        assert plugin._is_group_moderator(_Event(role="admin")) is True
        assert plugin._is_group_moderator(_Event(role="member")) is False

    async def test_god_tier_still_accepts_group_moderator(self, plugin):
        """群主/群管理保留诸神级权限（本次只收回管理级）。"""
        assert await plugin._check_perm(_Event(sender_id="555", role="owner")) is True
        assert await plugin._check_perm(_Event(sender_id="555", role="member")) is False
        assert await plugin._check_perm(_Event(sender_id="999", role="member")) is True

    def test_group_staff_is_super_admin_or_moderator(self, plugin):
        assert plugin._is_group_staff(_Event(sender_id="999", role="member")) is True
        assert plugin._is_group_staff(_Event(sender_id="555", role="owner")) is True
        assert plugin._is_group_staff(_Event(sender_id="555", role="member")) is False


class TestManagementGatesRejectGroupRoles:
    """管理闸门必须把群角色挡在外面。"""

    async def test_whitelist_command_denied_for_group_owner(self, plugin):
        event = _Event(sender_id="555", role="owner", text="白名单 add 12345")
        replies = [r async for r in plugin._whitelist_impl(event)]
        assert replies == [PERMISSION_DENIED["god_only"]]

    async def test_whitelist_command_allowed_for_super_admin(self, plugin):
        event = _Event(sender_id="999", role="member", text="白名单 add 12345")
        replies = [r async for r in plugin._whitelist_impl(event)]
        assert replies and "12345" in replies[0]
        assert await plugin.db_manager.is_whitelisted("12345") is True

    async def test_clear_denied_for_group_owner(self, plugin):
        event = _Event(sender_id="555", role="owner", text="天梯榜管理 清空 确认")
        replies = [r async for r in plugin._admin_impl(event)]
        assert replies == [PERMISSION_DENIED["god_only"]]

    async def test_delete_still_allowed_for_group_owner(self, plugin):
        """删除玩家属诸神级动作，群主保留。"""
        event = _Event(sender_id="555", role="owner", text="天梯榜管理 删除 张三")
        replies = [r async for r in plugin._admin_impl(event)]
        assert replies and PERMISSION_DENIED["god_only"] not in replies
        assert "张三" in replies[0]

    async def test_sync_whitelist_denied_for_group_owner(self, plugin):
        event = _Event(sender_id="555", role="owner", text="同步白名单")
        replies = [r async for r in plugin._sync_whitelist_impl(event)]
        assert replies == [PERMISSION_DENIED["god_only"]]


class TestDeprecatedConfigWhitelist:
    """WebUI 的 whitelist 配置已废弃：启动时告警一次，且完全不授权。"""

    async def test_stale_config_warns_and_grants_nothing(self, stubbed_astrbot, caplog):
        import logging

        import astrbot_plugin_faith_ladder.main as m

        plugin = m.FaithLadderPlugin(m.Context(), {
            "admin_ids": [],
            "whitelist": [{"type": "user", "id": "555"}],
        })
        try:
            with caplog.at_level(logging.WARNING):
                await plugin.initialize()

            assert any("废弃" in r.getMessage() for r in caplog.records), \
                "残留的 WebUI 白名单配置应产生一条告警"
            assert await plugin.permission_service.check_score_permission("555") is False
        finally:
            await plugin.terminate()
