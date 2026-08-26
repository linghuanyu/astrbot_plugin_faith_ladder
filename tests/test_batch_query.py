"""
Tests for batch query feature.
"""

import pytest
import pytest_asyncio


@pytest.mark.asyncio
class TestBatchQuery:
    """Tests for batch player query."""

    async def test_get_players_by_names_empty(self, db_manager):
        """Empty names list returns empty dict."""
        result = await db_manager.get_players_by_names("g1", [])
        assert result == {}

    async def test_get_players_by_names_single(self, db_manager):
        """Single name query works."""
        await db_manager.upsert_player("g1", "u1", "Alice")
        result = await db_manager.get_players_by_names("g1", ["Alice"])
        assert len(result) == 1
        assert "Alice" in result
        assert result["Alice"].player_name == "Alice"

    async def test_get_players_by_names_multiple(self, db_manager):
        """Multiple names query works."""
        await db_manager.upsert_player("g1", "u1", "Alice")
        await db_manager.upsert_player("g1", "u2", "Bob")
        await db_manager.upsert_player("g1", "u3", "Charlie")
        result = await db_manager.get_players_by_names("g1", ["Alice", "Bob", "Charlie"])
        assert len(result) == 3
        assert "Alice" in result
        assert "Bob" in result
        assert "Charlie" in result

    async def test_get_players_by_names_partial(self, db_manager):
        """Partial match returns only existing players."""
        await db_manager.upsert_player("g1", "u1", "Alice")
        await db_manager.upsert_player("g1", "u2", "Bob")
        result = await db_manager.get_players_by_names("g1", ["Alice", "Bob", "NotExist"])
        assert len(result) == 2
        assert "Alice" in result
        assert "Bob" in result
        assert "NotExist" not in result

    async def test_get_players_by_names_different_groups(self, db_manager):
        """Different groups have independent player data."""
        await db_manager.upsert_player("g1", "u1", "Alice")
        await db_manager.upsert_player("g2", "u2", "Bob")
        result = await db_manager.get_players_by_names("g1", ["Alice", "Bob"])
        assert len(result) == 1
        assert "Alice" in result
        assert "Bob" not in result  # Bob is in g2, not g1


@pytest.mark.asyncio
class TestBatchQueryService:
    """Tests for batch query in ladder_service."""

    async def test_get_player_cards_by_names_empty(self, db_manager):
        """Empty names returns empty result."""
        from astrbot_plugin_faith_ladder.ladder_service import LadderService
        service = LadderService(db_manager)
        cards, not_found = await service.get_player_cards_by_names("g1", [])
        assert cards == ""
        assert not_found == []

    async def test_get_player_cards_by_names_all_not_found(self, db_manager):
        """All names not found returns not_found list."""
        from astrbot_plugin_faith_ladder.ladder_service import LadderService
        service = LadderService(db_manager)
        cards, not_found = await service.get_player_cards_by_names("g1", ["Alice", "Bob"])
        assert cards == ""
        assert sorted(not_found) == ["Alice", "Bob"]

    async def test_get_player_cards_by_names_partial(self, db_manager):
        """Partial match returns cards for existing players and not_found for others."""
        from astrbot_plugin_faith_ladder.ladder_service import LadderService
        service = LadderService(db_manager)
        await db_manager.upsert_player("g1", "u1", "Alice")
        cards, not_found = await service.get_player_cards_by_names("g1", ["Alice", "NotExist"])
        assert "Alice" in cards
        assert "NotExist" in not_found

    async def test_get_player_cards_by_names_multiple(self, db_manager):
        """Multiple players returns combined cards."""
        from astrbot_plugin_faith_ladder.ladder_service import LadderService
        service = LadderService(db_manager)
        await db_manager.upsert_player("g1", "u1", "Alice")
        await db_manager.upsert_player("g1", "u2", "Bob")
        cards, not_found = await service.get_player_cards_by_names("g1", ["Alice", "Bob"])
        assert "Alice" in cards
        assert "Bob" in cards
        assert not_found == []
