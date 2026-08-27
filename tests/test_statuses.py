"""
Tests for player status system.
"""

import pytest
import tempfile
from pathlib import Path
from datetime import datetime, timedelta

from astrbot_plugin_faith_ladder.db_manager import DatabaseManager
from astrbot_plugin_faith_ladder.ladder_service import LadderService
from astrbot_plugin_faith_ladder.message_formatter import format_player_card
from astrbot_plugin_faith_ladder.models import Player


class TestDatabaseStatuses:
    """Tests for DB status CRUD methods."""

    @pytest.fixture
    async def db(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dbm = DatabaseManager(Path(tmpdir))
            await dbm.initialize()
            yield dbm
            await dbm.close()

    @pytest.mark.asyncio
    async def test_add_status(self, db):
        await db.upsert_player("g1", "u1", "Alice")
        await db.add_status("g1", "u1", "虚弱", 3)
        statuses = await db.get_player_statuses("g1", "u1")
        assert len(statuses) == 1
        assert statuses[0]["status_name"] == "虚弱"
        assert statuses[0]["remaining_days"] == 3

    @pytest.mark.asyncio
    async def test_add_multiple_statuses(self, db):
        await db.upsert_player("g1", "u1", "Alice")
        await db.add_status("g1", "u1", "虚弱", 3)
        await db.add_status("g1", "u1", "护盾", 7)
        statuses = await db.get_player_statuses("g1", "u1")
        assert len(statuses) == 2

    @pytest.mark.asyncio
    async def test_add_status_zero_days_ignored(self, db):
        await db.upsert_player("g1", "u1", "Alice")
        await db.add_status("g1", "u1", "虚弱", 0)
        statuses = await db.get_player_statuses("g1", "u1")
        assert len(statuses) == 0

    @pytest.mark.asyncio
    async def test_remove_status(self, db):
        await db.upsert_player("g1", "u1", "Alice")
        await db.add_status("g1", "u1", "虚弱", 3)
        result = await db.remove_status("g1", "u1", "虚弱")
        assert result is True
        statuses = await db.get_player_statuses("g1", "u1")
        assert len(statuses) == 0

    @pytest.mark.asyncio
    async def test_remove_nonexistent_status(self, db):
        await db.upsert_player("g1", "u1", "Alice")
        result = await db.remove_status("g1", "u1", "幽灵状态")
        assert result is False

    @pytest.mark.asyncio
    async def test_clear_statuses(self, db):
        await db.upsert_player("g1", "u1", "Alice")
        await db.add_status("g1", "u1", "虚弱", 3)
        await db.add_status("g1", "u1", "护盾", 7)
        count = await db.clear_statuses("g1", "u1")
        assert count == 2
        statuses = await db.get_player_statuses("g1", "u1")
        assert len(statuses) == 0

    @pytest.mark.asyncio
    async def test_expired_status_not_returned(self, db):
        """Expired statuses should not be returned by get_player_statuses."""
        await db.upsert_player("g1", "u1", "Alice")
        # Insert a status that expires in the past
        past = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        await db._db.execute(
            "INSERT INTO player_statuses (group_id, player_id, status_name, expire_at) "
            "VALUES (?, ?, ?, ?)",
            ("g1", "u1", "已过期", past)
        )
        await db._db.commit()
        statuses = await db.get_player_statuses("g1", "u1")
        assert len(statuses) == 0

    @pytest.mark.asyncio
    async def test_purge_expired_statuses(self, db):
        await db.upsert_player("g1", "u1", "Alice")
        # Insert expired status
        past = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        await db._db.execute(
            "INSERT INTO player_statuses (group_id, player_id, status_name, expire_at) "
            "VALUES (?, ?, ?, ?)",
            ("g1", "u1", "已过期", past)
        )
        # Insert valid status
        await db.add_status("g1", "u1", "护盾", 7)
        await db._db.commit()

        deleted = await db.purge_expired_statuses()
        assert deleted == 1

        statuses = await db.get_player_statuses("g1", "u1")
        assert len(statuses) == 1
        assert statuses[0]["status_name"] == "护盾"

    @pytest.mark.asyncio
    async def test_delete_player_cleans_statuses(self, db):
        """Deleting a player should also delete their statuses."""
        await db.upsert_player("g1", "u1", "Alice")
        await db.add_status("g1", "u1", "虚弱", 3)
        await db.delete_player("g1", "u1")
        statuses = await db.get_player_statuses("g1", "u1")
        assert len(statuses) == 0


class TestFormatPlayerCardWithStatuses:
    """Tests for format_player_card with statuses."""

    def test_card_with_statuses(self):
        player = Player(
            player_id="u1", group_id="g1", player_name="Alice",
            class_="战士", faith="虚无",
            ladder_score=1200, pilgrimage_score=150
        )
        statuses = [
            {"status_name": "虚弱", "remaining_days": 2},
            {"status_name": "护盾", "remaining_days": 5},
            {"status_name": "中毒", "remaining_days": 0},
        ]
        result = format_player_card(player, ladder_rank=3, pilgrimage_rank=2, statuses=statuses)
        assert "─── 状态 ───" in result
        assert "虚弱: 剩余2天" in result
        assert "护盾: 剩余5天" in result
        assert "中毒: 今日到期" in result

    def test_card_without_statuses(self):
        player = Player(
            player_id="u1", group_id="g1", player_name="Alice",
            ladder_score=1200, pilgrimage_score=150
        )
        result = format_player_card(player, ladder_rank=3, pilgrimage_rank=2, statuses=[])
        assert "─── 状态 ───" not in result

    def test_card_with_none_statuses(self):
        player = Player(
            player_id="u1", group_id="g1", player_name="Alice",
            ladder_score=1200, pilgrimage_score=150
        )
        result = format_player_card(player, ladder_rank=3, pilgrimage_rank=2)
        assert "─── 状态 ───" not in result


class TestGiveAndTakeStatuses:
    """Tests for ladder_service status methods."""

    @pytest.fixture
    async def service(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = DatabaseManager(Path(tmpdir))
            await db.initialize()
            svc = LadderService(db)
            yield svc
            await db.close()

    @pytest.mark.asyncio
    async def test_add_status(self, service):
        await service.db.upsert_player("g1", "u1", "Alice")
        success, msg = await service.add_status("g1", "Alice", "虚弱", 3)
        assert success is True
        assert "虚弱" in msg
        assert "3天" in msg

    @pytest.mark.asyncio
    async def test_add_status_nonexistent_player(self, service):
        success, msg = await service.add_status("g1", "Ghost", "虚弱", 3)
        assert success is False
        assert "不存在" in msg

    @pytest.mark.asyncio
    async def test_remove_status(self, service):
        await service.db.upsert_player("g1", "u1", "Alice")
        await service.db.add_status("g1", "u1", "虚弱", 3)
        success, msg = await service.remove_status("g1", "Alice", "虚弱")
        assert success is True
        assert "移除" in msg

    @pytest.mark.asyncio
    async def test_remove_nonexistent_status(self, service):
        await service.db.upsert_player("g1", "u1", "Alice")
        success, msg = await service.remove_status("g1", "Alice", "幽灵")
        assert success is False
        assert "没有" in msg

    @pytest.mark.asyncio
    async def test_clear_statuses(self, service):
        await service.db.upsert_player("g1", "u1", "Alice")
        await service.db.add_status("g1", "u1", "虚弱", 3)
        await service.db.add_status("g1", "u1", "护盾", 7)
        success, msg = await service.clear_statuses("g1", "Alice")
        assert success is True
        assert "2" in msg
