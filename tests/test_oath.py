"""
Tests for abandon_oath (弃誓) and set_faith (立誓) functionality.
"""

import pytest
import tempfile
from pathlib import Path

from astrbot_plugin_faith_ladder.db_manager import DatabaseManager
from astrbot_plugin_faith_ladder.ladder_service import LadderService
from astrbot_plugin_faith_ladder.models import Player


class TestAbandonOath:
    """Tests for abandon_oath()."""

    @pytest.fixture
    async def service(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = DatabaseManager(Path(tmpdir))
            await db.initialize()
            svc = LadderService(db)
            yield svc
            await db.close()

    CONFIG = {
        "oath_text_虚无": "{name}背弃了虚无之道。万法归寂，誓约已碎。",
        "oath_text_存在": "{name}否认了存在之意。所信皆空，誓约已碎。",
        "oath_text_文明": "{name}舍弃了文明之光。秩序崩塌，誓约已碎。",
        "oath_text_沉沦": "{name}离开了沉沦之渊。深渊不再接纳，誓约已碎。",
        "oath_text_混沌": "{name}背离了混沌之源。混乱不再眷顾，誓约已碎。",
        "oath_text_生命": "{name}抛弃了生命之根。生机断绝，誓约已碎。",
    }

    @pytest.mark.asyncio
    async def test_abandon_oath_with_new_faith(self, service):
        """Test abandoning oath and setting new faith."""
        await service.db.upsert_player("g1", "u1", "Alice")
        await service.db.set_player_class("g1", "u1", "法师", "虚无")

        success, msg = await service.abandon_oath("g1", "Alice", "文明", self.CONFIG)
        assert success is True
        assert "背弃了虚无之道" in msg
        assert "文明" in msg

        player = await service.db.get_player("g1", "u1")
        assert player.oathbreaker is True
        assert player.faith == "文明"

    @pytest.mark.asyncio
    async def test_abandon_oath_without_new_faith(self, service):
        """Test abandoning oath without changing faith."""
        await service.db.upsert_player("g1", "u1", "Alice")
        await service.db.set_player_class("g1", "u1", "法师", "存在")

        success, msg = await service.abandon_oath("g1", "Alice", None, self.CONFIG)
        assert success is True
        assert "弃誓者" in msg

        player = await service.db.get_player("g1", "u1")
        assert player.oathbreaker is True
        assert player.faith == "存在"  # unchanged

    @pytest.mark.asyncio
    async def test_abandon_oath_no_faith(self, service):
        """Test abandoning oath when player has no faith."""
        await service.db.upsert_player("g1", "u1", "Alice")

        success, msg = await service.abandon_oath("g1", "Alice", None, self.CONFIG)
        assert success is False
        assert "尚无信仰" in msg

    @pytest.mark.asyncio
    async def test_abandon_oath_nonexistent_player(self, service):
        """Test abandoning oath for non-existent player."""
        success, msg = await service.abandon_oath("g1", "Ghost", None, self.CONFIG)
        assert success is False
        assert "不存在" in msg

    @pytest.mark.asyncio
    async def test_abandon_oath_invalid_faith(self, service):
        """Test abandoning oath with invalid new faith."""
        await service.db.upsert_player("g1", "u1", "Alice")
        await service.db.set_player_class("g1", "u1", "法师", "虚无")

        success, msg = await service.abandon_oath("g1", "Alice", "无效信仰", self.CONFIG)
        assert success is False
        assert "无效信仰" in msg


class TestSetFaith:
    """Tests for set_faith()."""

    @pytest.fixture
    async def service(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = DatabaseManager(Path(tmpdir))
            await db.initialize()
            svc = LadderService(db)
            yield svc
            await db.close()

    @pytest.mark.asyncio
    async def test_set_faith_normal_player(self, service):
        """Test setting faith for a normal player."""
        await service.db.upsert_player("g1", "u1", "Alice")

        success, msg = await service.set_faith("g1", "Alice", "虚无")
        assert success is True
        assert "虚无" in msg

        player = await service.db.get_player("g1", "u1")
        assert player.faith == "虚无"
        assert player.oathbreaker is False

    @pytest.mark.asyncio
    async def test_set_faith_oathbreaker_keeps_tag(self, service):
        """Test that setting faith for oathbreaker keeps the tag."""
        await service.db.upsert_player("g1", "u1", "Alice")
        await service.db.set_player_class("g1", "u1", "法师", "虚无")
        await service.db.set_oathbreaker("g1", "u1", None)

        success, msg = await service.set_faith("g1", "Alice", "文明")
        assert success is True
        assert "弃誓者" in msg

        player = await service.db.get_player("g1", "u1")
        assert player.faith == "文明"
        assert player.oathbreaker is True  # still an oathbreaker

    @pytest.mark.asyncio
    async def test_set_faith_invalid(self, service):
        """Test setting invalid faith."""
        await service.db.upsert_player("g1", "u1", "Alice")
        success, msg = await service.set_faith("g1", "Alice", "无效")
        assert success is False
        assert "无效信仰" in msg


class TestClearOathbreaker:
    """Tests for clear_oathbreaker()."""

    @pytest.fixture
    async def db_manager(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = DatabaseManager(Path(tmpdir))
            await db.initialize()
            yield db
            await db.close()

    @pytest.mark.asyncio
    async def test_clear_oathbreaker(self, db_manager):
        """Test clearing oathbreaker status."""
        await db_manager.upsert_player("g1", "u1", "Alice")
        await db_manager.set_oathbreaker("g1", "u1", None)

        player = await db_manager.get_player("g1", "u1")
        assert player.oathbreaker is True

        await db_manager.clear_oathbreaker("g1", "u1")

        player = await db_manager.get_player("g1", "u1")
        assert player.oathbreaker is False


class TestOathbreakerDisplay:
    """Tests for oathbreaker display in formatter."""

    def test_leaderboard_shows_tag(self):
        """Test that leaderboard shows (弃誓者) tag."""
        from astrbot_plugin_faith_ladder.message_formatter import format_leaderboard

        players = [
            Player(player_id="u1", group_id="g1", player_name="Alice",
                   class_="法师", faith="虚无", ladder_score=1000,
                   pilgrimage_score=200, oathbreaker=True),
            Player(player_id="u2", group_id="g1", player_name="Bob",
                   class_="战士", faith="存在", ladder_score=800,
                   pilgrimage_score=100, oathbreaker=False),
        ]
        result = format_leaderboard(players, 10)
        assert "Alice(弃誓者)" in result
        assert "Bob(弃誓者)" not in result

    def test_player_card_shows_tag(self):
        """Test that player card shows (弃誓者) tag."""
        from astrbot_plugin_faith_ladder.message_formatter import format_player_card

        player = Player(player_id="u1", group_id="g1", player_name="Alice",
                        class_="法师", faith="虚无", ladder_score=1000,
                        pilgrimage_score=200, oathbreaker=True)
        result = format_player_card(player)
        assert "Alice(弃誓者)" in result
