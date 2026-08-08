"""
Tests for gift item system (赠送道具).
"""

import pytest
import tempfile
from pathlib import Path

from astrbot_plugin_faith_ladder.db_manager import DatabaseManager
from astrbot_plugin_faith_ladder.ladder_service import LadderService


class TestDeductAndReceiveItems:
    """Tests for deduct_item and receive_item."""

    @pytest.fixture
    async def service(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = DatabaseManager(Path(tmpdir))
            await db.initialize()
            svc = LadderService(db)
            yield svc
            await db.close()

    @pytest.mark.asyncio
    async def test_deduct_item_success(self, service):
        await service.db.upsert_player("g1", "u1", "Alice")
        await service.db.add_item("g1", "u1", "铁剑", 5)
        success, msg = await service.deduct_item("g1", "u1", "Alice", "铁剑", 3)
        assert success is True
        items = await service.db.get_player_items("g1", "u1")
        assert items[0]["quantity"] == 2

    @pytest.mark.asyncio
    async def test_deduct_item_insufficient(self, service):
        await service.db.upsert_player("g1", "u1", "Alice")
        await service.db.add_item("g1", "u1", "铁剑", 2)
        success, msg = await service.deduct_item("g1", "u1", "Alice", "铁剑", 5)
        assert success is False
        assert "不足" in msg

    @pytest.mark.asyncio
    async def test_receive_item(self, service):
        await service.db.upsert_player("g1", "u1", "Bob")
        success, msg = await service.receive_item("g1", "u1", "Bob", "铁剑", 3)
        assert success is True
        items = await service.db.get_player_items("g1", "u1")
        assert len(items) == 1
        assert items[0]["item_name"] == "铁剑"
        assert items[0]["quantity"] == 3

    @pytest.mark.asyncio
    async def test_full_gift_flow(self, service):
        """Test complete gift flow: deduct from sender, receive by receiver."""
        await service.db.upsert_player("g1", "u1", "Alice")
        await service.db.upsert_player("g1", "u2", "Bob")
        await service.db.add_item("g1", "u1", "铁剑", 5)

        # Sender deducts
        success, _ = await service.deduct_item("g1", "u1", "Alice", "铁剑", 3)
        assert success is True

        # Receiver receives
        success, _ = await service.receive_item("g1", "u2", "Bob", "铁剑", 3)
        assert success is True

        # Verify
        alice_items = await service.db.get_player_items("g1", "u1")
        bob_items = await service.db.get_player_items("g1", "u2")
        assert alice_items[0]["quantity"] == 2
        assert bob_items[0]["quantity"] == 3
