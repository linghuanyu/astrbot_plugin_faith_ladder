"""
Tests for the ladder service business logic.
"""

import pytest
from astrbot_plugin_faith_ladder.ladder_service import LadderService


@pytest.mark.asyncio
class TestLadderService:
    """Tests for LadderService."""

    async def test_get_leaderboard_empty(self, db_manager):
        """Test leaderboard with no players."""
        service = LadderService(db_manager)
        text = await service.get_leaderboard_text("g1", 10)
        assert "暂无排名数据" in text

    async def test_get_leaderboard_with_players(self, db_manager):
        """Test leaderboard with players."""
        service = LadderService(db_manager)

        await db_manager.upsert_player("g1", "u1", "Alice")
        await db_manager.upsert_player("g1", "u2", "Bob")
        await db_manager.update_scores("g1", "u1", 100, 50, "admin")
        await db_manager.update_scores("g1", "u2", 200, 30, "admin")

        text = await service.get_leaderboard_text("g1", 10)
        assert "1. Bob" in text
        assert "2. Alice" in text
        assert "天梯积分" in text
        assert "觐见之梯" in text

    async def test_get_player_card(self, db_manager):
        """Test getting player card."""
        service = LadderService(db_manager)
        await db_manager.upsert_player("g1", "u1", "Alice")
        await db_manager.set_player_class("g1", "u1", "法师", "存在")

        text = await service.get_player_card_text("g1", "u1")
        assert text is not None
        assert "Alice" in text
        assert "法师" in text
        assert "存在" in text

    async def test_get_player_card_not_found(self, db_manager):
        """Test getting player card for non-existent player."""
        service = LadderService(db_manager)
        text = await service.get_player_card_text("g1", "u999")
        assert text is None

    async def test_get_player_card_by_name(self, db_manager):
        """Test getting player card by name."""
        service = LadderService(db_manager)
        await db_manager.upsert_player("g1", "u1", "Alice")
        await db_manager.update_scores("g1", "u1", 100, 50, "admin")

        text = await service.get_player_card_by_name("g1", "Alice")
        assert text is not None
        assert "Alice" in text

    async def test_add_score_new_player(self, db_manager):
        """Test adding score to a new player."""
        service = LadderService(db_manager)
        success, msg = await service.add_score("g1", "u1", "Alice", 100, 50, "admin")
        assert success is True
        assert "Alice" in msg
        assert "100" in msg or "+100" in msg

    async def test_add_score_existing_player(self, db_manager):
        """Test adding score to an existing player."""
        service = LadderService(db_manager)
        await db_manager.upsert_player("g1", "u1", "Alice")
        await db_manager.update_scores("g1", "u1", 100, 50, "admin")

        success, msg = await service.add_score("g1", "u1", "Alice", 30, 20, "admin")
        assert success is True

        player = await db_manager.get_player("g1", "u1")
        assert player.ladder_score == 1130  # 1000 initial + 100 + 30
        assert player.pilgrimage_score == 170  # 100 initial + 50 + 20

    async def test_set_class_valid(self, db_manager):
        """Test setting valid class and faith."""
        service = LadderService(db_manager)
        await db_manager.upsert_player("g1", "u1", "Alice")

        success, msg = await service.set_class("g1", "u1", "Alice", "法师", "存在")
        assert success is True
        assert "法师" in msg
        assert "存在" in msg

    async def test_set_class_invalid_class(self, db_manager):
        """Test setting invalid class."""
        service = LadderService(db_manager)
        await db_manager.upsert_player("g1", "u1", "Alice")

        success, msg = await service.set_class("g1", "u1", "Alice", "无效职业", "存在")
        assert success is False
        assert "无效职业" in msg

    async def test_set_class_invalid_faith(self, db_manager):
        """Test setting invalid faith."""
        service = LadderService(db_manager)
        await db_manager.upsert_player("g1", "u1", "Alice")

        success, msg = await service.set_class("g1", "u1", "Alice", "法师", "无效信仰")
        assert success is False
        assert "无效信仰" in msg

    async def test_set_class_auto_creates_player(self, db_manager):
        """Test that set_class creates player if not exists."""
        service = LadderService(db_manager)

        success, msg = await service.set_class("g1", "u1", "NewPlayer", "战士", "虚无")
        assert success is True

        player = await db_manager.get_player("g1", "u1")
        assert player is not None
        assert player.class_ == "战士"
        assert player.faith == "虚无"
