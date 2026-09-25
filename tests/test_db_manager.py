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
        """Test creating a new player via upsert.

        新建玩家应返回数据库里的真实记录（含初始分）。旧实现新建时返回一个只填了
        三个字段的临时 Player（分数为数据类默认值 0），改名时才回读数据库（分数为
        初始分 1000），同一个函数因名字是否变化而返回不同来源的对象。
        """
        player = await db_manager.upsert_player("g1", "u1", "TestPlayer")
        assert player.player_id == "u1"
        assert player.group_id == "g1"
        assert player.player_name == "TestPlayer"
        assert player.ladder_score == 1000
        assert player.pilgrimage_score == 100
        assert player.class_ is None
        assert player.faith is None

    async def test_upsert_player_concurrent_same_name(self, db_manager):
        """并发 upsert 同一玩家不应抛 IntegrityError，且只留一条记录。"""
        import asyncio

        results = await asyncio.gather(
            db_manager.upsert_player("g1", "u1", "Alice"),
            db_manager.upsert_player("g1", "u1", "Alice"),
        )
        assert all(p is not None for p in results)
        players = await db_manager.get_all_players_in_group("g1")
        assert len(players) == 1

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
        """Test getting all global whitelist entries. Group type is filtered out (deprecated)."""
        await db_manager.add_to_whitelist("user", "u1", "admin")
        await db_manager.add_to_whitelist("user", "u2", "admin")
        await db_manager.add_to_whitelist("group", "g1", "admin")

        entries = await db_manager.get_whitelist()
        # Group entries are filtered out (deprecated)
        assert len(entries) == 2

    async def test_get_whitelist_empty(self, db_manager):
        """Test getting whitelist when empty."""
        entries = await db_manager.get_whitelist()
        assert entries == []


@pytest.mark.asyncio
class TestEnsureColumn:
    """加列迁移的唯一实现（`initialize()` 里那张声明式表走的都是它）。"""

    async def test_adds_missing_column_then_skips(self, db_manager):
        """返回本次是否新增：第一次 True，之后 False（每次启动都会重跑一遍）。"""
        await db_manager._db.execute("CREATE TABLE scratch (a TEXT)")
        await db_manager._db.commit()

        assert await db_manager._ensure_column("scratch", "b", "TEXT DEFAULT NULL") is True
        assert await db_manager._ensure_column("scratch", "b", "TEXT DEFAULT NULL") is False

    async def test_failure_message_names_table_and_column(self, db_manager):
        """加列失败要报出表名与列名，而不是一句裸的 sqlite 错误。

        这几列都是读查询依赖的列，缺了会变成"插件能启动、但每条命令都抛错"，
        所以这里中断启动是刻意的，但报错必须能直接指向该查什么。
        """
        with pytest.raises(RuntimeError) as excinfo:
            await db_manager._ensure_column("no_such_table", "x", "TEXT")

        message = str(excinfo.value)
        assert "no_such_table" in message
        assert "x" in message


@pytest.mark.asyncio
@pytest.mark.asyncio
class TestBackup:
    """Tests for database backup."""

    async def test_backup_to_produces_valid_snapshot(self, db_manager, temp_data_dir):
        """备份应是自洽的数据库快照，且内容可读（VACUUM INTO）。

        此前用 shutil.copy2 拷活动中的 .db 文件，不拷 -journal，
        可能在有未提交写入时得到撕裂副本。
        """
        await db_manager.upsert_player("g1", "u1", "TestPlayer")

        backup_dir = temp_data_dir / "backups"
        backup_path = backup_dir / "ladder_backup_test.db"
        await db_manager.backup_to(backup_path)

        assert backup_path.exists()
        import sqlite3
        con = sqlite3.connect(backup_path)
        try:
            names = [r[0] for r in con.execute("SELECT player_name FROM players").fetchall()]
        finally:
            con.close()
        assert names == ["TestPlayer"]

    async def test_backup_to_overwrites_existing_file(self, db_manager, temp_data_dir):
        """目标文件已存在时应先删除再生成，不留残余。"""
        backup_dir = temp_data_dir / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / "ladder_backup_test.db"
        backup_path.write_text("stale", encoding="utf-8")

        await db_manager.backup_to(backup_path)
        assert backup_path.stat().st_size > len("stale")
