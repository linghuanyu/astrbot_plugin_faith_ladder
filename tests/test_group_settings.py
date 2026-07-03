"""
Tests for group settings and output mode functionality in db_manager.
"""

import pytest


@pytest.mark.asyncio
class TestGroupSettings:
    """Tests for per-group output mode settings."""

    async def test_get_group_output_mode_not_set(self, db_manager):
        """Test getting output mode when not set returns None."""
        mode = await db_manager.get_group_output_mode("g1")
        assert mode is None

    async def test_set_and_get_group_output_mode_text(self, db_manager):
        """Test setting and getting text output mode."""
        await db_manager.set_group_output_mode("g1", "text")
        mode = await db_manager.get_group_output_mode("g1")
        assert mode == "text"

    async def test_set_and_get_group_output_mode_image(self, db_manager):
        """Test setting and getting image output mode."""
        await db_manager.set_group_output_mode("g1", "image")
        mode = await db_manager.get_group_output_mode("g1")
        assert mode == "image"

    async def test_set_group_output_mode_overwrite(self, db_manager):
        """Test overwriting existing output mode."""
        await db_manager.set_group_output_mode("g1", "text")
        await db_manager.set_group_output_mode("g1", "image")
        mode = await db_manager.get_group_output_mode("g1")
        assert mode == "image"

    async def test_clear_group_output_mode(self, db_manager):
        """Test clearing output mode (passing invalid value)."""
        await db_manager.set_group_output_mode("g1", "image")
        await db_manager.set_group_output_mode("g1", "clear")
        mode = await db_manager.get_group_output_mode("g1")
        assert mode is None

    async def test_group_isolation(self, db_manager):
        """Test that output modes are isolated per group."""
        await db_manager.set_group_output_mode("g1", "image")
        await db_manager.set_group_output_mode("g2", "text")
        assert await db_manager.get_group_output_mode("g1") == "image"
        assert await db_manager.get_group_output_mode("g2") == "text"

    async def test_multiple_groups_independent(self, db_manager):
        """Test that different groups can have different modes."""
        await db_manager.set_group_output_mode("g1", "image")
        await db_manager.set_group_output_mode("g2", "text")
        await db_manager.set_group_output_mode("g3", "image")

        assert await db_manager.get_group_output_mode("g1") == "image"
        assert await db_manager.get_group_output_mode("g2") == "text"
        assert await db_manager.get_group_output_mode("g3") == "image"


@pytest.mark.asyncio
class TestScoreHistoryRetention:
    """Tests for score history purge functionality."""

    async def test_purge_old_score_history(self, db_manager):
        """Test purging old score history entries."""
        # Create a player and some score history
        await db_manager.upsert_player("g1", "u1", "TestPlayer")
        await db_manager.update_scores("g1", "u1", 100, 50, "admin", "test")

        # Purge with very short retention (should not delete recent entries)
        deleted = await db_manager.purge_old_score_history(retention_days=1)
        assert deleted == 0  # Entry was just created

    async def test_purge_empty_history(self, db_manager):
        """Test purging when no history exists."""
        deleted = await db_manager.purge_old_score_history(retention_days=90)
        assert deleted == 0
