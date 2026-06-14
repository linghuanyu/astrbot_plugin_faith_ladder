"""
Database manager for the faith ladder plugin.
Handles all SQLite operations using aiosqlite.
"""

import aiosqlite
import shutil
from pathlib import Path
from typing import List, Optional
from datetime import datetime

from astrbot_plugin_faith_ladder.models import Player, ScoreEntry


class DatabaseManager:
    """Manages all database operations for the faith ladder plugin."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.db_path = data_dir / "ladder.db"
        self._initialized = False

    async def initialize(self):
        """Create database and tables if they don't exist."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        await self._create_tables()
        self._initialized = True

    async def _create_tables(self):
        """Create all required database tables."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS players (
                    player_id TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    player_name TEXT NOT NULL,
                    class TEXT DEFAULT NULL,
                    faith TEXT DEFAULT NULL,
                    ladder_score INTEGER DEFAULT 0,
                    pilgrimage_score INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (player_id, group_id)
                );
                CREATE INDEX IF NOT EXISTS idx_players_ladder
                    ON players(group_id, ladder_score DESC);
                CREATE INDEX IF NOT EXISTS idx_players_name
                    ON players(group_id, player_name);

                CREATE TABLE IF NOT EXISTS score_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    player_id TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    ladder_change INTEGER DEFAULT 0,
                    pilgrimage_change INTEGER DEFAULT 0,
                    reason TEXT,
                    operator_id TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_score_history_player
                    ON score_history(group_id, player_id);

                CREATE TABLE IF NOT EXISTS whitelist (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entry_type TEXT NOT NULL,
                    entry_id TEXT NOT NULL,
                    added_by TEXT,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(entry_type, entry_id)
                );
                CREATE INDEX IF NOT EXISTS idx_whitelist_lookup
                    ON whitelist(entry_type, entry_id);

                CREATE TABLE IF NOT EXISTS active_groups (
                    group_id TEXT PRIMARY KEY,
                    last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            await db.commit()

            # Migrate old whitelist table (had group_id column) to global whitelist
            await self._migrate_whitelist(db)

    async def _migrate_whitelist(self, db):
        """Migrate whitelist table from per-group to global if needed."""
        async with db.execute("PRAGMA table_info(whitelist)") as cursor:
            columns = [row[1] for row in await cursor.fetchall()]

        if "group_id" in columns:
            # Old schema detected: recreate without group_id, deduplicate
            await db.execute("""
                CREATE TABLE IF NOT EXISTS whitelist_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entry_type TEXT NOT NULL,
                    entry_id TEXT NOT NULL,
                    added_by TEXT,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(entry_type, entry_id)
                )
            """)
            await db.execute("""
                INSERT OR IGNORE INTO whitelist_new (entry_type, entry_id, added_by, added_at)
                SELECT DISTINCT entry_type, entry_id, added_by, added_at FROM whitelist
            """)
            await db.execute("DROP TABLE whitelist")
            await db.execute("ALTER TABLE whitelist_new RENAME TO whitelist")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_whitelist_lookup ON whitelist(entry_type, entry_id)")
            await db.commit()

    async def upsert_player(
        self, group_id: str, player_id: str, player_name: str,
        initial_ladder: int = 1000, initial_pilgrimage: int = 100
    ) -> Player:
        """Create or update a player record. New players get initial scores."""
        async with aiosqlite.connect(self.db_path) as db:
            # Check if player exists
            async with db.execute(
                "SELECT player_id, group_id, player_name, class, faith, ladder_score, pilgrimage_score, created_at, updated_at FROM players WHERE player_id = ? AND group_id = ?",
                (player_id, group_id)
            ) as cursor:
                row = await cursor.fetchone()

            if row:
                # Update name if changed
                if row[2] != player_name:
                    await db.execute(
                        "UPDATE players SET player_name = ?, updated_at = CURRENT_TIMESTAMP WHERE player_id = ? AND group_id = ?",
                        (player_name, player_id, group_id)
                    )
                    await db.commit()
                    async with db.execute(
                        "SELECT player_id, group_id, player_name, class, faith, ladder_score, pilgrimage_score, created_at, updated_at FROM players WHERE player_id = ? AND group_id = ?",
                        (player_id, group_id)
                    ) as cursor2:
                        updated_row = await cursor2.fetchone()
                    return Player(
                        player_id=updated_row[0], group_id=updated_row[1], player_name=updated_row[2],
                        class_=updated_row[3], faith=updated_row[4], ladder_score=updated_row[5],
                        pilgrimage_score=updated_row[6], created_at=updated_row[7], updated_at=updated_row[8]
                    )
                return Player(
                    player_id=row[0], group_id=row[1], player_name=row[2],
                    class_=row[3], faith=row[4], ladder_score=row[5],
                    pilgrimage_score=row[6], created_at=row[7], updated_at=row[8]
                )
            else:
                # Create new player with initial scores
                await db.execute(
                    "INSERT INTO players (player_id, group_id, player_name, ladder_score, pilgrimage_score) VALUES (?, ?, ?, ?, ?)",
                    (player_id, group_id, player_name, initial_ladder, initial_pilgrimage)
                )
                await db.commit()
                return Player(
                    player_id=player_id, group_id=group_id,
                    player_name=player_name
                )

    async def get_player(self, group_id: str, player_id: str) -> Optional[Player]:
        """Get a player by ID and group."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT player_id, group_id, player_name, class, faith, ladder_score, pilgrimage_score, created_at, updated_at FROM players WHERE player_id = ? AND group_id = ?",
                (player_id, group_id)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                return Player(
                    player_id=row[0], group_id=row[1], player_name=row[2],
                    class_=row[3], faith=row[4], ladder_score=row[5],
                    pilgrimage_score=row[6], created_at=row[7], updated_at=row[8]
                )

    async def get_player_by_name(self, group_id: str, player_name: str) -> Optional[Player]:
        """Get a player by name and group (case-sensitive)."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT player_id, group_id, player_name, class, faith, ladder_score, pilgrimage_score, created_at, updated_at FROM players WHERE group_id = ? AND player_name = ?",
                (group_id, player_name)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                return Player(
                    player_id=row[0], group_id=row[1], player_name=row[2],
                    class_=row[3], faith=row[4], ladder_score=row[5],
                    pilgrimage_score=row[6], created_at=row[7], updated_at=row[8]
                )

    async def get_top_players(self, group_id: str, limit: int = 10) -> List[Player]:
        """Get top players by ladder score for a group."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT player_id, group_id, player_name, class, faith, ladder_score, pilgrimage_score, created_at, updated_at FROM players WHERE group_id = ? ORDER BY ladder_score DESC LIMIT ?",
                (group_id, limit)
            ) as cursor:
                rows = await cursor.fetchall()
                return [
                    Player(
                        player_id=r[0], group_id=r[1], player_name=r[2],
                        class_=r[3], faith=r[4], ladder_score=r[5],
                        pilgrimage_score=r[6], created_at=r[7], updated_at=r[8]
                    )
                    for r in rows
                ]

    async def get_top_players_by_pilgrimage(self, group_id: str, limit: int = 10) -> List[Player]:
        """Get top players by pilgrimage score for a group."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT player_id, group_id, player_name, class, faith, ladder_score, pilgrimage_score, created_at, updated_at FROM players WHERE group_id = ? ORDER BY pilgrimage_score DESC LIMIT ?",
                (group_id, limit)
            ) as cursor:
                rows = await cursor.fetchall()
                return [
                    Player(
                        player_id=r[0], group_id=r[1], player_name=r[2],
                        class_=r[3], faith=r[4], ladder_score=r[5],
                        pilgrimage_score=r[6], created_at=r[7], updated_at=r[8]
                    )
                    for r in rows
                ]

    async def update_scores(
        self, group_id: str, player_id: str,
        ladder_delta: int, pilgrimage_delta: int,
        operator_id: str, reason: str = ""
    ) -> Optional[Player]:
        """Update a player's scores and record history. Returns updated player or None if not found."""
        async with aiosqlite.connect(self.db_path) as db:
            # Check player exists
            async with db.execute(
                "SELECT player_id FROM players WHERE player_id = ? AND group_id = ?",
                (player_id, group_id)
            ) as cursor:
                if not await cursor.fetchone():
                    return None

            # Update scores
            await db.execute(
                "UPDATE players SET ladder_score = ladder_score + ?, pilgrimage_score = pilgrimage_score + ?, updated_at = CURRENT_TIMESTAMP WHERE player_id = ? AND group_id = ?",
                (ladder_delta, pilgrimage_delta, player_id, group_id)
            )

            # Record history
            await db.execute(
                "INSERT INTO score_history (player_id, group_id, ladder_change, pilgrimage_change, reason, operator_id) VALUES (?, ?, ?, ?, ?, ?)",
                (player_id, group_id, ladder_delta, pilgrimage_delta, reason, operator_id)
            )

            await db.commit()

            # Return updated player
            return await self.get_player(group_id, player_id)

    async def set_player_class(
        self, group_id: str, player_id: str,
        class_name: str, faith_name: str
    ) -> Optional[Player]:
        """Set a player's class and faith. Returns updated player or None if not found."""
        async with aiosqlite.connect(self.db_path) as db:
            # Check player exists
            async with db.execute(
                "SELECT player_id FROM players WHERE player_id = ? AND group_id = ?",
                (player_id, group_id)
            ) as cursor:
                if not await cursor.fetchone():
                    return None

            await db.execute(
                "UPDATE players SET class = ?, faith = ?, updated_at = CURRENT_TIMESTAMP WHERE player_id = ? AND group_id = ?",
                (class_name, faith_name, player_id, group_id)
            )
            await db.commit()
            return await self.get_player(group_id, player_id)

    # --- Player management operations ---

    async def delete_player(self, group_id: str, player_id: str) -> bool:
        """Delete a player and their score history. Returns True if deleted."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM players WHERE player_id = ? AND group_id = ?",
                (player_id, group_id)
            )
            await db.execute(
                "DELETE FROM score_history WHERE player_id = ? AND group_id = ?",
                (player_id, group_id)
            )
            await db.commit()
            return cursor.rowcount > 0

    async def delete_player_by_name(self, group_id: str, player_name: str) -> bool:
        """Delete a player by name. Returns True if deleted."""
        player = await self.get_player_by_name(group_id, player_name)
        if not player:
            return False
        return await self.delete_player(group_id, player.player_id)

    async def reset_all_scores(self, group_id: str, initial_ladder: int = 1000, initial_pilgrimage: int = 100) -> int:
        """Reset all players' scores to initial values. Returns number of players reset."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "UPDATE players SET ladder_score = ?, pilgrimage_score = ?, updated_at = CURRENT_TIMESTAMP WHERE group_id = ?",
                (initial_ladder, initial_pilgrimage, group_id)
            )
            await db.commit()
            return cursor.rowcount

    async def delete_all_players(self, group_id: str) -> int:
        """Delete all players and score history in a group. Returns number of players deleted."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM players WHERE group_id = ?", (group_id,)
            )
            await db.execute(
                "DELETE FROM score_history WHERE group_id = ?", (group_id,)
            )
            await db.commit()
            return cursor.rowcount

    # --- Global whitelist operations ---

    async def add_to_whitelist(
        self, entry_type: str, entry_id: str, added_by: str
    ) -> bool:
        """Add an entry to the global whitelist. Returns True if added, False if already exists."""
        async with aiosqlite.connect(self.db_path) as db:
            try:
                await db.execute(
                    "INSERT INTO whitelist (entry_type, entry_id, added_by) VALUES (?, ?, ?)",
                    (entry_type, entry_id, added_by)
                )
                await db.commit()
                return True
            except aiosqlite.IntegrityError:
                return False

    async def remove_from_whitelist(
        self, entry_type: str, entry_id: str
    ) -> bool:
        """Remove an entry from the global whitelist. Returns True if removed, False if not found."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM whitelist WHERE entry_type = ? AND entry_id = ?",
                (entry_type, entry_id)
            )
            await db.commit()
            return cursor.rowcount > 0

    async def is_whitelisted(self, user_id: str) -> bool:
        """Check if a user is in the global whitelist."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT 1 FROM whitelist WHERE entry_type = 'user' AND entry_id = ?",
                (user_id,)
            ) as cursor:
                return await cursor.fetchone() is not None

    async def get_whitelist(self) -> List[dict]:
        """Get all global whitelist entries."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT entry_type, entry_id, added_by, added_at FROM whitelist"
            ) as cursor:
                rows = await cursor.fetchall()
                return [
                    {
                        "entry_type": r[0],
                        "entry_id": r[1],
                        "added_by": r[2],
                        "added_at": r[3]
                    }
                    for r in rows
                ]

    # --- Active groups ---

    async def register_active_group(self, group_id: str):
        """Register a group as active (for daily push)."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO active_groups (group_id, last_active) VALUES (?, CURRENT_TIMESTAMP)",
                (group_id,)
            )
            await db.commit()

    async def get_active_groups(self) -> List[str]:
        """Get all active group IDs."""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute("SELECT group_id FROM active_groups") as cursor:
                rows = await cursor.fetchall()
                return [r[0] for r in rows]

    # --- Backup ---

    async def backup_database(self, backup_dir: Path) -> Path:
        """Create a backup of the database. Returns backup file path."""
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / f"ladder_backup_{timestamp}.db"
        shutil.copy2(self.db_path, backup_path)
        return backup_path

    async def cleanup_old_backups(self, backup_dir: Path, retention_days: int):
        """Remove backups older than retention_days."""
        if not backup_dir.exists():
            return

        cutoff = datetime.now().timestamp() - (retention_days * 86400)
        for f in backup_dir.glob("ladder_backup_*.db"):
            if f.stat().st_mtime < cutoff:
                f.unlink()

    async def close(self):
        """Clean up resources."""
        pass  # aiosqlite connections are per-operation, nothing to close
