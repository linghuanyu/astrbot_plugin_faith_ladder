"""
Tests for QQ binding anti-impersonation feature.
"""

import pytest
import pytest_asyncio


@pytest.mark.asyncio
class TestQQBindingDB:
    """Tests for the QQ binding DB operations."""

    async def test_get_player_by_qq_unbound(self, db_manager):
        """Unbound player returns None."""
        await db_manager.upsert_player("g1", "u1", "Alice")
        assert await db_manager.get_player_by_qq("g1", "123456") is None

    async def test_set_and_get_player_qq(self, db_manager):
        """set_player_qq then get_player_by_qq returns the player."""
        await db_manager.upsert_player("g1", "u1", "Alice")
        ok = await db_manager.set_player_qq("g1", "u1", "123456")
        assert ok is True
        player = await db_manager.get_player_by_qq("g1", "123456")
        assert player is not None
        assert player.player_id == "u1"
        assert player.qq_id == "123456"

    async def test_set_player_qq_unique_conflict(self, db_manager):
        """Binding a QQ already bound to another player returns False."""
        await db_manager.upsert_player("g1", "u1", "Alice")
        await db_manager.upsert_player("g1", "u2", "Bob")
        assert await db_manager.set_player_qq("g1", "u1", "123456") is True
        # Same QQ → different player should fail
        assert await db_manager.set_player_qq("g1", "u2", "123456") is False

    async def test_set_player_qq_same_player_idempotent(self, db_manager):
        """Re-binding same QQ to same player succeeds (idempotent)."""
        await db_manager.upsert_player("g1", "u1", "Alice")
        assert await db_manager.set_player_qq("g1", "u1", "123456") is True
        assert await db_manager.set_player_qq("g1", "u1", "123456") is True
        player = await db_manager.get_player_by_qq("g1", "123456")
        assert player.player_id == "u1"

    async def test_qq_binding_isolated_per_group(self, db_manager):
        """Same QQ can be bound to different players in different groups."""
        await db_manager.upsert_player("g1", "u1", "Alice")
        await db_manager.upsert_player("g2", "u2", "Bob")
        assert await db_manager.set_player_qq("g1", "u1", "123456") is True
        assert await db_manager.set_player_qq("g2", "u2", "123456") is True
        p1 = await db_manager.get_player_by_qq("g1", "123456")
        p2 = await db_manager.get_player_by_qq("g2", "123456")
        assert p1.player_id == "u1"
        assert p2.player_id == "u2"

    async def test_player_model_has_qq_id(self, db_manager):
        """Player model exposes qq_id after row_to_player."""
        await db_manager.upsert_player("g1", "u1", "Alice")
        p = await db_manager.get_player("g1", "u1")
        assert p.qq_id is None
        await db_manager.set_player_qq("g1", "u1", "999999")
        p = await db_manager.get_player("g1", "u1")
        assert p.qq_id == "999999"


@pytest.mark.asyncio
class TestRebindQQ:
    """Tests for rebind_player_qq."""

    async def test_rebind_changes_qq(self, db_manager):
        """Rebinding changes the player's QQ, old QQ becomes free."""
        await db_manager.upsert_player("g1", "u1", "Alice")
        await db_manager.set_player_qq("g1", "u1", "111111")
        ok, msg, old = await db_manager.rebind_player_qq("g1", "u1", "222222")
        assert ok is True
        assert old == "111111"
        # new QQ bound
        p = await db_manager.get_player_by_qq("g1", "222222")
        assert p.player_id == "u1"
        # old QQ free
        assert await db_manager.get_player_by_qq("g1", "111111") is None

    async def test_rebind_conflict_rejected(self, db_manager):
        """Rebinding to a QQ already bound to another player fails."""
        await db_manager.upsert_player("g1", "u1", "Alice")
        await db_manager.upsert_player("g1", "u2", "Bob")
        await db_manager.set_player_qq("g1", "u1", "111111")
        await db_manager.set_player_qq("g1", "u2", "222222")
        ok, msg, old = await db_manager.rebind_player_qq("g1", "u1", "222222")
        assert ok is False
        assert "Bob" in msg
        # Original bindings preserved
        assert (await db_manager.get_player_by_qq("g1", "111111")).player_id == "u1"
        assert (await db_manager.get_player_by_qq("g1", "222222")).player_id == "u2"

    async def test_rebind_same_qq_idempotent(self, db_manager):
        """Rebinding to the same QQ succeeds (no-op)."""
        await db_manager.upsert_player("g1", "u1", "Alice")
        await db_manager.set_player_qq("g1", "u1", "111111")
        ok, msg, old = await db_manager.rebind_player_qq("g1", "u1", "111111")
        assert ok is True
        assert old == "111111"
        p = await db_manager.get_player_by_qq("g1", "111111")
        assert p.player_id == "u1"

    async def test_rebind_unbound_player(self, db_manager):
        """Rebinding an unbound player acts as first-time bind."""
        await db_manager.upsert_player("g1", "u1", "Alice")
        ok, msg, old = await db_manager.rebind_player_qq("g1", "u1", "333333")
        assert ok is True
        assert old is None
        p = await db_manager.get_player_by_qq("g1", "333333")
        assert p.player_id == "u1"


@pytest.mark.asyncio
class TestRegisterPlayerWithQQ:
    """Test ladder_service.register_player qq_id parameter."""

    async def test_register_with_qq(self, db_manager):
        from astrbot_plugin_faith_ladder.ladder_service import LadderService
        service = LadderService(db_manager)
        ok, msg = await service.register_player(
            "g1", "Alice", "虚无", "战士", 1000, 100, "op1", qq_id="111111"
        )
        assert ok is True
        player = await db_manager.get_player_by_qq("g1", "111111")
        assert player is not None
        assert player.player_name == "Alice"

    async def test_register_with_duplicate_qq_fails(self, db_manager):
        from astrbot_plugin_faith_ladder.ladder_service import LadderService
        service = LadderService(db_manager)
        ok1, _ = await service.register_player(
            "g1", "Alice", "虚无", "战士", 1000, 100, "op1", qq_id="111111"
        )
        assert ok1 is True
        ok2, msg2 = await service.register_player(
            "g1", "Bob", "存在", "法师", 1000, 100, "op1", qq_id="111111"
        )
        assert ok2 is False
        assert "已被玩家 Alice 绑定" in msg2

    async def test_register_without_qq(self, db_manager):
        from astrbot_plugin_faith_ladder.ladder_service import LadderService
        service = LadderService(db_manager)
        ok, msg = await service.register_player(
            "g1", "Alice", "虚无", "战士", 1000, 100, "op1"
        )
        assert ok is True
        player = await db_manager.get_player_by_name("g1", "Alice")
        assert player.qq_id is None
