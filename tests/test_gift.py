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
        success, msg, base_name, grade = await service.deduct_item("g1", "u1", "Alice", "铁剑", 3)
        assert success is True
        items = await service.db.get_player_items("g1", "u1")
        assert items[0]["quantity"] == 2

    @pytest.mark.asyncio
    async def test_deduct_item_insufficient(self, service):
        await service.db.upsert_player("g1", "u1", "Alice")
        await service.db.add_item("g1", "u1", "铁剑", 2)
        success, msg, _, _ = await service.deduct_item("g1", "u1", "Alice", "铁剑", 5)
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
        success, _, _, _ = await service.deduct_item("g1", "u1", "Alice", "铁剑", 3)
        assert success is True

        # Receiver receives
        success, _ = await service.receive_item("g1", "u2", "Bob", "铁剑", 3)
        assert success is True

        # Verify
        alice_items = await service.db.get_player_items("g1", "u1")
        bob_items = await service.db.get_player_items("g1", "u2")
        assert alice_items[0]["quantity"] == 2
        assert bob_items[0]["quantity"] == 3


class TestPendingGiftSemantics:
    """待处理赠送的认领语义、超时退款与并发扣减。"""

    @pytest.fixture
    async def service(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = DatabaseManager(Path(tmpdir))
            await db.initialize()
            svc = LadderService(db)
            yield svc
            await db.close()

    @staticmethod
    def _items(item_name: str, quantity: int) -> str:
        import json
        return json.dumps({"item_name": item_name, "grade": None, "quantity": quantity})

    @pytest.mark.asyncio
    async def test_save_pending_gift_refuses_overwrite(self, service):
        """同一接收方的第二笔赠送不得覆盖第一笔，否则第一笔道具会凭空消失。"""
        first = await service.db.save_pending_gift(
            "g1", "u2", "u1", "Alice", "Bob", self._items("铁剑", 3)
        )
        assert first is True
        second = await service.db.save_pending_gift(
            "g1", "u2", "u3", "Carol", "Bob", self._items("盾牌", 1)
        )
        assert second is False

        kept = await service.db.get_pending_gift("g1", "u2")
        assert kept["sender_name"] == "Alice"
        assert kept["items"]["item_name"] == "铁剑"

    @pytest.mark.asyncio
    async def test_delete_pending_gift_claims_once(self, service):
        """删除要能区分"删到了"与"没删到"，并发下以此认领同一笔赠送。"""
        await service.db.save_pending_gift(
            "g1", "u2", "u1", "Alice", "Bob", self._items("铁剑", 1)
        )
        assert await service.db.delete_pending_gift("g1", "u2") is True
        assert await service.db.delete_pending_gift("g1", "u2") is False

    @pytest.mark.asyncio
    async def test_cleanup_expired_gifts_refunds_once(self, service):
        """超时退款：退还发送方、回调清理内存缓存，重复执行不重复退款。"""
        await service.db.upsert_player("g1", "u1", "Alice")
        await service.db.save_pending_gift(
            "g1", "u2", "u1", "Alice", "Bob", self._items("铁剑", 3)
        )

        forgotten = []
        refunded = await service.cleanup_expired_gifts(
            max_age_seconds=-1, on_refunded=lambda g, r: forgotten.append((g, r))
        )
        assert refunded == 1
        assert forgotten == [("g1", "u2")]
        assert await service.db.get_pending_gift("g1", "u2") is None

        sender_items = await service.db.get_player_items("g1", "u1")
        assert sender_items[0]["quantity"] == 3

        # 再跑一次：记录已认领，不应重复退款
        assert await service.cleanup_expired_gifts(max_age_seconds=-1) == 0
        assert (await service.db.get_player_items("g1", "u1"))[0]["quantity"] == 3

    @pytest.mark.asyncio
    async def test_remove_item_require_sufficient_keeps_data(self, service):
        """严格模式下数量不足时不得改动数据（旧默认行为是截断到 0）。"""
        await service.db.upsert_player("g1", "u1", "Alice")
        await service.db.add_item("g1", "u1", "铁剑", 2)

        assert await service.db.remove_item("g1", "u1", "铁剑", 5, require_sufficient=True) is False
        items = await service.db.get_player_items("g1", "u1")
        assert items[0]["quantity"] == 2

    @pytest.mark.asyncio
    async def test_concurrent_deduction_only_one_wins(self, service):
        """并发扣同一件道具时只有一个成功，不会凭空多出道具。"""
        import asyncio
        await service.db.upsert_player("g1", "u1", "Alice")
        await service.db.add_item("g1", "u1", "铁剑", 1)

        results = await asyncio.gather(
            service.deduct_item("g1", "u1", "Alice", "铁剑", 1),
            service.deduct_item("g1", "u1", "Alice", "铁剑", 1),
        )
        assert sum(1 for ok, _, _, _ in results if ok) == 1
        items = await service.db.get_player_items("g1", "u1")
        assert items == [] or items[0]["quantity"] == 0
