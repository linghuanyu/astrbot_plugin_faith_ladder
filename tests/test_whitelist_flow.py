"""
白名单流程测试：入群进待审 → 超管审核 → 退群自动撤销，以及手动同步。

背景：入群曾经直接把人写成诸神，而白名单是**全局**的——"谁能进这个群"就等于
"谁能成为诸神"。现在入群只产生待审记录，必须超管确认才授权。
"""

import contextlib

from astrbot_plugin_faith_ladder.messages import PERMISSION_DENIED


class _Sender:
    def __init__(self, role):
        self.role = role


class _MessageObj:
    def __init__(self, role, group_id, raw_message=None):
        self.sender = _Sender(role)
        self.group_id = group_id
        if raw_message is not None:
            self.raw_message = raw_message


class _Event:
    """指令与 notice 事件共用的最小事件。"""

    def __init__(self, sender_id="999", role="member", group_id="100",
                 text="", notice=None, self_id="1"):
        self._sender_id = sender_id
        self._self_id = self_id
        self.message_obj = _MessageObj(role, group_id, raw_message=notice)
        self.message_str = text

    def get_sender_id(self):
        return self._sender_id

    def get_self_id(self):
        return self._self_id

    def plain_result(self, text):
        return text


def _notice(notice_type, group_id, user_id):
    return {"notice_type": notice_type, "group_id": group_id, "user_id": user_id}


@contextlib.asynccontextmanager
async def _plugin(config):
    import astrbot_plugin_faith_ladder.main as m

    p = m.FaithLadderPlugin(m.Context(), config)
    await p.initialize()
    try:
        yield p
    finally:
        await p.terminate()


AUTO_GROUP = "777"
CONFIG = {"admin_ids": ["999"], "auto_whitelist_group": AUTO_GROUP}


class TestJoinCreatesPending:
    async def test_join_is_pending_not_authorised(self, stubbed_astrbot):
        async with _plugin(CONFIG) as p:
            await p._group_member_change_impl(
                _Event(notice=_notice("group_increase", AUTO_GROUP, "555"))
            )

            assert await p.db_manager.is_whitelisted("555") is False, \
                "入群不应直接授权"
            assert [r["entry_id"] for r in await p.db_manager.get_pending_whitelist()] == ["555"]

    async def test_join_ignores_other_groups_and_bot_itself(self, stubbed_astrbot):
        async with _plugin(CONFIG) as p:
            # 别的群：完全不动
            await p._group_member_change_impl(
                _Event(notice=_notice("group_increase", "888", "555"))
            )
            # 机器人自己入群：也不记
            await p._group_member_change_impl(
                _Event(notice=_notice("group_increase", AUTO_GROUP, "1"))
            )
            assert await p.db_manager.get_pending_whitelist() == []


class TestReviewCommands:
    async def _pending(self, p, uid):
        await p._group_member_change_impl(
            _Event(notice=_notice("group_increase", AUTO_GROUP, uid))
        )

    async def test_pending_listing(self, stubbed_astrbot):
        async with _plugin(CONFIG) as p:
            await self._pending(p, "555")
            replies = [r async for r in p._whitelist_impl(_Event(text="白名单 待审"))]
            assert replies and "555" in replies[0]

    async def test_approve_grants_and_clears_pending(self, stubbed_astrbot):
        async with _plugin(CONFIG) as p:
            await self._pending(p, "555")
            replies = [r async for r in p._whitelist_impl(_Event(text="白名单 通过 555"))]
            assert replies and "555" in replies[0]
            assert await p.db_manager.is_whitelisted("555") is True
            assert await p.db_manager.get_pending_whitelist() == []

    async def test_approve_unknown_id_reports(self, stubbed_astrbot):
        async with _plugin(CONFIG) as p:
            replies = [r async for r in p._whitelist_impl(_Event(text="白名单 通过 000"))]
            assert replies and "待审" in replies[0]

    async def test_approve_all(self, stubbed_astrbot):
        async with _plugin(CONFIG) as p:
            await self._pending(p, "555")
            await self._pending(p, "556")
            replies = [r async for r in p._whitelist_impl(_Event(text="白名单 全部通过"))]
            assert replies and "2" in replies[0]
            assert await p.db_manager.is_whitelisted("555") is True
            assert await p.db_manager.is_whitelisted("556") is True
            assert await p.db_manager.get_pending_whitelist() == []

    async def test_reject_discards_without_granting(self, stubbed_astrbot):
        async with _plugin(CONFIG) as p:
            await self._pending(p, "555")
            replies = [r async for r in p._whitelist_impl(_Event(text="白名单 拒绝 555"))]
            assert replies and "已拒绝" in replies[0]
            assert await p.db_manager.is_whitelisted("555") is False
            assert await p.db_manager.get_pending_whitelist() == []

    async def test_approve_cleans_redundant_pending_row(self, stubbed_astrbot):
        """同一 id 既有 user 又有 pending（历史遗留）时，转正应把冗余待审行清掉。"""
        async with _plugin(CONFIG) as p:
            await p.db_manager.add_to_whitelist("user", "555", "999")
            # 直接落库构造出这种并存状态：新代码的守卫不会再造，但旧数据里可能有
            await p.db_manager.add_to_whitelist("pending", "555", "auto")
            replies = [r async for r in p._whitelist_impl(_Event(text="白名单 通过 555"))]

            assert replies and "已通过" in replies[0]
            assert await p.db_manager.is_whitelisted("555") is True
            assert await p.db_manager.get_pending_whitelist() == []

    async def test_review_commands_denied_for_group_owner(self, stubbed_astrbot):
        async with _plugin(CONFIG) as p:
            await self._pending(p, "555")
            event = _Event(sender_id="556", role="owner", text="白名单 全部通过")
            replies = [r async for r in p._whitelist_impl(event)]
            assert replies == [PERMISSION_DENIED["god_only"]]
            assert await p.db_manager.is_whitelisted("555") is False


class TestLeaveRevokes:
    async def test_leave_removes_god_and_pending(self, stubbed_astrbot):
        async with _plugin(CONFIG) as p:
            # 一位已转正的诸神，另留一条待审
            await p.db_manager.add_to_whitelist("user", "555", "999")
            await p.db_manager.add_to_whitelist("pending", "555", "auto")

            await p._group_member_change_impl(
                _Event(notice=_notice("group_decrease", AUTO_GROUP, "555"))
            )

            assert await p.db_manager.is_whitelisted("555") is False
            assert await p.db_manager.get_pending_whitelist() == []

    async def test_leave_clears_cached_permission(self, stubbed_astrbot):
        """退群后缓存必须失效，否则被撤权的人还能继续通过。"""
        async with _plugin(CONFIG) as p:
            await p.db_manager.add_to_whitelist("user", "555", "999")
            assert await p.permission_service.check_score_permission("555") is True

            await p._group_member_change_impl(
                _Event(notice=_notice("group_decrease", AUTO_GROUP, "555"))
            )
            assert await p.permission_service.check_score_permission("555") is False


class TestPendingWriteGuard:
    """已是诸神的人不该再拿到待审行——唯一键只管 (entry_type, entry_id)，挡不住这个。"""

    async def test_existing_god_join_creates_no_pending(self, stubbed_astrbot):
        async with _plugin(CONFIG) as p:
            await p.db_manager.add_to_whitelist("user", "555", "999")
            await p._group_member_change_impl(
                _Event(notice=_notice("group_increase", AUTO_GROUP, "555"))
            )
            assert await p.db_manager.get_pending_whitelist() == []

    async def test_sync_skips_existing_gods(self, stubbed_astrbot):
        async with _plugin(CONFIG) as p:
            await p.db_manager.add_to_whitelist("user", "555", "999")
            replies = await _sync(p, "同步白名单", ["555", "556"])

            assert "1 人进入待审" in replies[0]
            assert [r["entry_id"] for r in await p.db_manager.get_pending_whitelist()] == ["556"]


class TestManualSyncGoesToPending:
    """同步不再直接授权：否则「同步白名单」一条命令就把全群封神，待审形同虚设。"""

    async def test_manual_sync_creates_pending_not_gods(self, stubbed_astrbot):
        async with _plugin(CONFIG) as p:
            replies = await _sync(p, "同步白名单", ["555", "1"])  # 1 是机器人自己

            assert replies and "1 人进入待审" in replies[0]
            assert await p.db_manager.is_whitelisted("555") is False
            assert [r["entry_id"] for r in await p.db_manager.get_pending_whitelist()] == ["555"]

    async def test_sync_then_approve_authorises(self, stubbed_astrbot):
        async with _plugin(CONFIG) as p:
            await _sync(p, "同步白名单", ["555"])
            replies = [r async for r in p._whitelist_impl(_Event(text="白名单 全部通过"))]

            assert "已通过 1 人" in replies[0]
            assert await p.db_manager.is_whitelisted("555") is True
            assert await p.db_manager.get_pending_whitelist() == []


async def _sync(plugin, text, member_ids):
    """跑一次「同步白名单」，成员列表固定为给定 id。"""
    class _Bot:
        async def get_group_member_list(self, group_id):
            return [{"user_id": uid} for uid in member_ids]

    event = _Event(text=text)
    event.bot = _Bot()
    return [r async for r in plugin._sync_whitelist_impl(event)]


class TestBulkApproveThreshold:
    """超过 5 人的一次通过要先看名单再确认；预览路径必须不落库。"""

    async def _seed(self, p, n):
        for i in range(n):
            await p.db_manager.add_pending(f"60{i}", "auto")

    async def test_exactly_five_approves_directly(self, stubbed_astrbot):
        async with _plugin(CONFIG) as p:
            await self._seed(p, 5)
            replies = [r async for r in p._whitelist_impl(_Event(text="白名单 全部通过"))]

            assert "已通过 5 人" in replies[0]
            assert await p.db_manager.get_pending_whitelist() == []
            assert await p.db_manager.is_whitelisted("600") is True

    async def test_six_requires_confirmation_and_writes_nothing(self, stubbed_astrbot):
        async with _plugin(CONFIG) as p:
            await self._seed(p, 6)
            replies = [r async for r in p._whitelist_impl(_Event(text="白名单 全部通过"))]

            assert "确认" in replies[0]
            assert "6 人" in replies[0]
            # 这是全部意义所在：预览不落库，没有任何人被授权
            assert len(await p.db_manager.get_pending_whitelist()) == 6
            assert await p.db_manager.is_whitelisted("600") is False

            replies = [r async for r in p._whitelist_impl(_Event(text="白名单 全部通过 确认"))]
            assert "已通过 6 人" in replies[0]
            assert await p.db_manager.get_pending_whitelist() == []
            assert await p.db_manager.is_whitelisted("600") is True

    async def test_reply_echoes_who_was_approved(self, stubbed_astrbot):
        async with _plugin(CONFIG) as p:
            await self._seed(p, 3)
            replies = [r async for r in p._whitelist_impl(_Event(text="白名单 全部通过"))]
            assert "600" in replies[0] and "602" in replies[0]


class TestRemoveAlsoClearsPending:
    async def test_remove_clears_pending_row(self, stubbed_astrbot):
        """remove 的语义是「把这个 id 拿掉」，待审行不该留下。"""
        async with _plugin(CONFIG) as p:
            await p.db_manager.add_to_whitelist("user", "555", "999")
            await p.db_manager.add_to_whitelist("pending", "555", "auto")

            replies = [r async for r in p._whitelist_impl(_Event(text="白名单 remove 555"))]

            assert replies and "移除" in replies[0]
            assert await p.db_manager.is_whitelisted("555") is False
            assert await p.db_manager.get_pending_whitelist() == []


class TestSyncPrune:
    """默认只增不删；「清理」只清由群同步写入的条目，手动加的诸神不动。"""

    async def test_batch_sync_dedupes_and_counts(self, stubbed_astrbot):
        async with _plugin(CONFIG) as p:
            replies = await _sync(p, "同步白名单", ["555", "555", "556"])
            assert "2 人进入待审" in replies[0]
            assert [r["entry_id"] for r in await p.db_manager.get_pending_whitelist()] == ["555", "556"]

    async def test_default_sync_never_removes(self, stubbed_astrbot):
        async with _plugin(CONFIG) as p:
            await p.db_manager.add_to_whitelist("user", "900", "auto")  # 已不在群里
            await _sync(p, "同步白名单", ["555"])
            assert await p.db_manager.is_whitelisted("900") is True

    async def test_prune_removes_synced_absent_but_keeps_manual(self, stubbed_astrbot):
        async with _plugin(CONFIG) as p:
            await p.db_manager.add_to_whitelist("user", "900", "auto")   # 群同步写入，已退群
            await p.db_manager.add_to_whitelist("user", "901", "999")    # 手动加的，不在群
            replies = await _sync(p, "同步白名单 清理", ["555"])

            assert await p.db_manager.is_whitelisted("900") is False
            assert await p.db_manager.is_whitelisted("901") is True, \
                "手动添加的诸神不该被「清理」抹掉"
            assert "移除 1 人" in replies[0]

    async def test_prune_also_clears_stale_pending(self, stubbed_astrbot):
        """离线期间退群的人可能在待审里留下残影，同样按群成员对账掉。"""
        async with _plugin(CONFIG) as p:
            await p.db_manager.add_to_whitelist("pending", "902", "auto")  # 已不在群
            replies = await _sync(p, "同步白名单 清理", ["555"])

            pending_ids = [r["entry_id"] for r in await p.db_manager.get_pending_whitelist()]
            assert "902" not in pending_ids, "已不在群的待审残影应被清掉"
            assert "555" in pending_ids, "群里的人不该被清理"
            assert "移除 1 人" in replies[0]
