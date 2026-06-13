"""
Tests for the database manager.
"""

import pytest
import pytest_asyncio
from pathlib import Path


@pytest.mark.asyncio
class TestDatabaseManager:
    """Tests for DatabaseManager CRUD operations."""

    async def test_initialize_creates_database(self, db_manager, temp_data_dir):
        """Test that initialize creates the database file."""
        db_path = temp_data_dir / "ladder.db"
        assert db_path.exists()

    async def test_upsert_player_creates_new(self, db_manager):
        """Test creating a new player via upsert."""
        player = await db_manager.upsert_player("g1", "u1", "TestPlayer")
        assert player.player_id == "u1"
        assert player.group_id == "g1"
        assert player.player_name == "TestPlayer"
        assert player.ladder_score == 0
        assert player.pilgrimage_score == 0
        assert player.class_ is None
        assert player.faith is None

    async def test_upsert_player_updates_name(self, db_manager):
        """Test that upsert updates player name if changed."""
        await db_manager.upsert_player("g1", "u1", "OldName")
        player = await db_manager.upsert_player("g1", "u1", "NewName")
        assert player.player_name == "NewName"
        assert player.ladder_score == 1000  # Initial score preserved

    async def test_upsert_player_same_name(self, db_manager):
        """Test upsert with same name doesn't change anything."""
        await db_manager.upsert_player("g1", "u1", "TestPlayer")
        player = await db_manager.upsert_player("g1", "u1", "TestPlayer")
        assert player.player_name == "TestPlayer"

    async def test_get_player_exists(self, db_manager):
        """Test getting an existing player."""
        await db_manager.upsert_player("g1", "u1", "TestPlayer")
        player = await db_manager.get_player("g1", "u1")
        assert player is not None
        assert player.player_id == "u1"
        assert player.player_name == "TestPlayer"

    async def test_get_player_not_exists(self, db_manager):
        """Test getting a non-existent player returns None."""
        player = await db_manager.get_player("g1", "u999")
        assert player is None

    async def test_get_player_wrong_group(self, db_manager):
        """Test that get_player respects group boundary."""
        await db_manager.upsert_player("g1", "u1", "TestPlayer")
        player = await db_manager.get_player("g2", "u1")
        assert player is None

    async def test_get_player_by_name(self, db_manager):
        """Test getting a player by name."""
        await db_manager.upsert_player("g1", "u1", "Alice")
        player = await db_manager.get_player_by_name("g1", "Alice")
        assert player is not None
        assert player.player_id == "u1"

    async def test_get_player_by_name_not_found(self, db_manager):
        """Test getting a non-existent player by name."""
        player = await db_manager.get_player_by_name("g1", "NonExistent")
        assert player is None

    async def test_get_top_players_empty(self, db_manager):
        """Test getting top players from empty group."""
        players = await db_manager.get_top_players("g1", 10)
        assert players == []

    async def test_get_top_players_sorted(self, db_manager):
        """Test that top players are sorted by ladder_score descending."""
        await db_manager.upsert_player("g1", "u1", "Alice")
        await db_manager.upsert_player("g1", "u2", "Bob")
        await db_manager.upsert_player("g1", "u3", "Charlie")

        await db_manager.update_scores("g1", "u1", 100, 0, "admin")
        await db_manager.update_scores("g1", "u2", 300, 0, "admin")
        await db_manager.update_scores("g1", "u3", 200, 0, "admin")

        players = await db_manager.get_top_players("g1", 10)
        assert len(players) == 3
        assert players[0].player_name == "Bob"     # 300
        assert players[1].player_name == "Charlie"  # 200
        assert players[2].player_name == "Alice"    # 100

    async def test_get_top_players_limit(self, db_manager):
        """Test that top players respects the limit."""
        for i in range(5):
            await db_manager.upsert_player("g1", f"u{i}", f"Player{i}")
            await db_manager.update_scores("g1", f"u{i}", i * 100, 0, "admin")

        players = await db_manager.get_top_players("g1", 3)
        assert len(players) == 3

    async def test_get_top_players_group_isolation(self, db_manager):
        """Test that top players only returns players from the specified group."""
        await db_manager.upsert_player("g1", "u1", "Alice")
        await db_manager.upsert_player("g2", "u2", "Bob")

        await db_manager.update_scores("g1", "u1", 100, 0, "admin")
        await db_manager.update_scores("g2", "u2", 200, 0, "admin")

        g1_players = await db_manager.get_top_players("g1", 10)
        assert len(g1_players) == 1
        assert g1_players[0].player_name == "Alice"

    async def test_update_scores(self, db_manager):
        """Test updating player scores."""
        await db_manager.upsert_player("g1", "u1", "TestPlayer")
        updated = await db_manager.update_scores("g1", "u1", 100, 50, "admin")

        assert updated is not None
        assert updated.ladder_score == 1100  # 1000 initial + 100
        assert updated.pilgrimage_score == 150  # 100 initial + 50

    async def test_update_scores_accumulates(self, db_manager):
        """Test that score updates accumulate."""
        await db_manager.upsert_player("g1", "u1", "TestPlayer")
        await db_manager.update_scores("g1", "u1", 100, 50, "admin")
        updated = await db_manager.update_scores("g1", "u1", 30, -20, "admin")

        assert updated.ladder_score == 1130  # 1000 + 100 + 30
        assert updated.pilgrimage_score == 130  # 100 + 50 - 20

    async def test_update_scores_nonexistent_player(self, db_manager):
        """Test updating scores for non-existent player returns None."""
        result = await db_manager.update_scores("g1", "u999", 100, 50, "admin")
        assert result is None

    async def test_update_scores_negative(self, db_manager):
        """Test negative score changes."""
        await db_manager.upsert_player("g1", "u1", "TestPlayer")
        await db_manager.update_scores("g1", "u1", 100, 50, "admin")
        updated = await db_manager.update_scores("g1", "u1", -30, -20, "admin")

        assert updated.ladder_score == 1070  # 1000 + 100 - 30
        assert updated.pilgrimage_score == 130  # 100 + 50 - 20

    async def test_set_player_class(self, db_manager):
        """Test setting player class and faith."""
        await db_manager.upsert_player("g1", "u1", "TestPlayer")
        updated = await db_manager.set_player_class("g1", "u1", "法师", "存在")

        assert updated is not None
        assert updated.class_ == "法师"
        assert updated.faith == "存在"

    async def test_set_player_class_nonexistent(self, db_manager):
        """Test setting class for non-existent player."""
        result = await db_manager.set_player_class("g1", "u999", "法师", "存在")
        assert result is None

    async def test_set_player_class_overwrite(self, db_manager):
        """Test overwriting existing class and faith."""
        await db_manager.upsert_player("g1", "u1", "TestPlayer")
        await db_manager.set_player_class("g1", "u1", "法师", "存在")
        updated = await db_manager.set_player_class("g1", "u1", "战士", "虚无")

        assert updated.class_ == "战士"
        assert updated.faith == "虚无"


@pytest.mark.asyncio
class TestWhitelistOperations:
    """Tests for global whitelist CRUD operations."""

    async def test_add_to_whitelist(self, db_manager):
        """Test adding to global whitelist."""
        result = await db_manager.add_to_whitelist("user", "u123", "admin")
        assert result is True

    async def test_add_to_whitelist_duplicate(self, db_manager):
        """Test adding duplicate entry returns False."""
        await db_manager.add_to_whitelist("user", "u123", "admin")
        result = await db_manager.add_to_whitelist("user", "u123", "admin")
        assert result is False

    async def test_remove_from_whitelist(self, db_manager):
        """Test removing from global whitelist."""
        await db_manager.add_to_whitelist("user", "u123", "admin")
        result = await db_manager.remove_from_whitelist("user", "u123")
        assert result is True

    async def test_remove_from_whitelist_not_found(self, db_manager):
        """Test removing non-existent entry returns False."""
        result = await db_manager.remove_from_whitelist("user", "u999")
        assert result is False

    async def test_is_whitelisted_user(self, db_manager):
        """Test checking user whitelist status (global)."""
        await db_manager.add_to_whitelist("user", "u123", "admin")
        assert await db_manager.is_whitelisted("u123") is True
        assert await db_manager.is_whitelisted("u456") is False

    async def test_is_whitelisted_global(self, db_manager):
        """Test that whitelist is global - works regardless of which group user is in."""
        await db_manager.add_to_whitelist("user", "u123", "admin")
        # Same user should be whitelisted globally (no group concept)
        assert await db_manager.is_whitelisted("u123") is True

    async def test_get_whitelist(self, db_manager):
        """Test getting all global whitelist entries."""
        await db_manager.add_to_whitelist("user", "u1", "admin")
        await db_manager.add_to_whitelist("user", "u2", "admin")
        await db_manager.add_to_whitelist("group", "g1", "admin")

        entries = await db_manager.get_whitelist()
        assert len(entries) == 3

    async def test_get_whitelist_empty(self, db_manager):
        """Test getting whitelist when empty."""
        entries = await db_manager.get_whitelist()
        assert entries == []


@pytest.mark.asyncio
class TestActiveGroups:
    """Tests for active group tracking."""

    async def test_register_active_group(self, db_manager):
        """Test registering an active group."""
        await db_manager.register_active_group("g1")
        groups = await db_manager.get_active_groups()
        assert "g1" in groups

    async def test_register_multiple_groups(self, db_manager):
        """Test registering multiple active groups."""
        await db_manager.register_active_group("g1")
        await db_manager.register_active_group("g2")
        groups = await db_manager.get_active_groups()
        assert len(groups) == 2
        assert "g1" in groups
        assert "g2" in groups

    async def test_register_same_group_idempotent(self, db_manager):
        """Test that re-registering same group doesn't duplicate."""
        await db_manager.register_active_group("g1")
        await db_manager.register_active_group("g1")
        groups = await db_manager.get_active_groups()
        assert len(groups) == 1


@pytest.mark.asyncio
class TestBackup:
    """Tests for database backup operations."""

    async def test_backup_database(self, db_manager, temp_data_dir):
        """Test creating a database backup."""
        # Create some data first
        await db_manager.upsert_player("g1", "u1", "TestPlayer")

        backup_dir = temp_data_dir / "backups"
        backup_path = await db_manager.backup_database(backup_dir)

        assert backup_path.exists()
        assert backup_path.name.startswith("ladder_backup_")
        assert backup_path.name.endswith(".db")

    async def test_cleanup_old_backups(self, db_manager, temp_data_dir):
        """Test cleaning up old backups."""
        import time
        backup_dir = temp_data_dir / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)

        # Create a backup file with old modification time
        old_backup = backup_dir / "ladder_backup_20200101_000000.db"
        old_backup.write_text("old backup")

        # Set modification time to 30 days ago
        old_time = time.time() - (30 * 86400)
        import os
        os.utime(old_backup, (old_time, old_time))

        # Create a recent backup
        new_backup = backup_dir / "ladder_backup_20990101_000000.db"
        new_backup.write_text("new backup")

        # Cleanup with 7-day retention
        await db_manager.cleanup_old_backups(backup_dir, 7)

        assert not old_backup.exists()
        assert new_backup.exists()
