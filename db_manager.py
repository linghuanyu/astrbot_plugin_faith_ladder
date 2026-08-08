"""
Database manager for the faith ladder plugin.
Handles all SQLite operations using aiosqlite with a persistent connection.
"""

import asyncio
import aiosqlite
import shutil
from pathlib import Path
from typing import List, Optional
from datetime import datetime, timedelta, timezone

from astrbot_plugin_faith_ladder.models import Player

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)


class DatabaseManager:
    """Manages all database operations for the faith ladder plugin."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.db_path = data_dir / "ladder.db"
        self._db: Optional[aiosqlite.Connection] = None
        self._initialized = False

    async def initialize(self):
        """Create database and tables if they don't exist. Opens persistent connection."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        await self._create_tables()
        self._initialized = True

    async def _create_tables(self):
        """Create all required database tables and indexes."""
        await self._db.executescript("""
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
            CREATE INDEX IF NOT EXISTS idx_players_pilgrimage
                ON players(group_id, pilgrimage_score DESC);
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
            CREATE INDEX IF NOT EXISTS idx_score_history_ts
                ON score_history(timestamp);

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

            CREATE TABLE IF NOT EXISTS group_settings (
                group_id TEXT PRIMARY KEY,
                output_mode TEXT DEFAULT NULL
            );

            CREATE TABLE IF NOT EXISTS player_items (
                group_id TEXT NOT NULL,
                player_id TEXT NOT NULL,
                item_name TEXT NOT NULL,
                grade TEXT DEFAULT NULL,
                quantity INTEGER DEFAULT 1,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (group_id, player_id, item_name)
            );
            CREATE INDEX IF NOT EXISTS idx_items_player
                ON player_items(group_id, player_id);

            CREATE TABLE IF NOT EXISTS player_statuses (
                group_id TEXT NOT NULL,
                player_id TEXT NOT NULL,
                status_name TEXT NOT NULL,
                expire_at TIMESTAMP NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (group_id, player_id, status_name)
            );
            CREATE INDEX IF NOT EXISTS idx_statuses_player
                ON player_statuses(group_id, player_id);
            CREATE INDEX IF NOT EXISTS idx_statuses_expire
                ON player_statuses(expire_at);
        """)
        await self._db.commit()

        # Migrate old whitelist table (had group_id column) to global whitelist
        await self._migrate_whitelist()

        # Migrate: add oathbreaker column if missing
        await self._migrate_oathbreaker()

        # Migrate: clean up old item names with *N suffix
        await self._migrate_item_names()

        # Migrate: add grade column to player_items
        await self._migrate_items_add_grade()

    async def _migrate_oathbreaker(self):
        """Add oathbreaker column to players table if it doesn't exist."""
        async with self._db.execute("PRAGMA table_info(players)") as cursor:
            columns = [row[1] for row in await cursor.fetchall()]

        if "oathbreaker" not in columns:
            await self._db.execute(
                "ALTER TABLE players ADD COLUMN oathbreaker INTEGER DEFAULT 0"
            )
            await self._db.commit()

    async def _migrate_item_names(self):
        """Clean up old item names that have *N suffix (e.g., '糖果*3' -> '糖果')."""
        import re
        async with self._db.execute(
            "SELECT group_id, player_id, item_name, quantity FROM player_items"
        ) as cursor:
            rows = await cursor.fetchall()

        for group_id, player_id, item_name, quantity in rows:
            needs_migration = False

            # 情况1: 末尾是 ）*N（品级括号后跟数量，如 护身符（C级）*2）
            match_grade = re.match(r'^(.+）)\*(\d+)$', item_name)
            if match_grade:
                needs_migration = True

            # 情况2: 多个 *N 后缀（如 糖果*3*1）
            if not needs_migration:
                match_outer = re.match(r'^(.+)\*(\d+)$', item_name)
                if match_outer:
                    remaining = match_outer.group(1).strip()
                    match_inner = re.match(r'^(.+)\*(\d+)$', remaining)
                    if match_inner:
                        needs_migration = True

            if not needs_migration:
                continue

            # 有多个 *N，是旧 bug 数据，剥离所有尾部 *N
            clean_name = item_name
            total_multiplier = 1
            while True:
                m = re.match(r'^(.+)\*(\d+)$', clean_name)
                if not m:
                    break
                clean_name = m.group(1).strip()
                total_multiplier *= int(m.group(2))

            new_qty = quantity * total_multiplier
            # Check if clean name already exists
            async with self._db.execute(
                "SELECT quantity FROM player_items WHERE group_id = ? AND player_id = ? AND item_name = ?",
                (group_id, player_id, clean_name)
            ) as existing_cursor:
                existing = await existing_cursor.fetchone()

            if existing:
                # Merge quantities
                await self._db.execute(
                    "UPDATE player_items SET quantity = ? WHERE group_id = ? AND player_id = ? AND item_name = ?",
                    (existing[0] + new_qty, group_id, player_id, clean_name)
                )
                # Delete old record
                await self._db.execute(
                    "DELETE FROM player_items WHERE group_id = ? AND player_id = ? AND item_name = ?",
                    (group_id, player_id, item_name)
                )
            else:
                # Rename and update quantity
                await self._db.execute(
                    "UPDATE player_items SET item_name = ?, quantity = ? WHERE group_id = ? AND player_id = ? AND item_name = ?",
                    (clean_name, new_qty, group_id, player_id, item_name)
                )

        await self._db.commit()

    async def _migrate_items_add_grade(self):
        """Add grade column to player_items and backfill from item_name.
        Idempotent: safe to re-run if interrupted. ALTER TABLE is auto-committed
        in SQLite and can't be rolled back, so we separate column creation from
        data migration and make the data migration retryable."""
        async with self._db.execute("PRAGMA table_info(player_items)") as cursor:
            columns = [row[1] for row in await cursor.fetchall()]

        column_existed = "grade" in columns
        if not column_existed:
            await self._db.execute("ALTER TABLE player_items ADD COLUMN grade TEXT DEFAULT NULL")
            logger.info("[Migration] Added 'grade' column to player_items")

        from astrbot_plugin_faith_ladder.ladder_service import parse_item_full_name

        try:
            async with self._db.execute(
                "SELECT rowid, item_name FROM player_items"
            ) as cursor:
                rows = await cursor.fetchall()

            logger.info(f"[Migration] Scanning {len(rows)} rows for grade migration")
            migrated = 0
            for rowid, old_name in rows:
                base_name, grade = parse_item_full_name(old_name)
                # Only UPDATE if the name actually changed (had a grade pattern)
                # This makes the migration idempotent — already-migrated rows are skipped
                if base_name != old_name:
                    await self._db.execute(
                        "UPDATE player_items SET grade = ?, item_name = ? WHERE rowid = ?",
                        (grade, base_name, rowid)
                    )
                    migrated += 1
                    logger.info(f"[Migration] Row {rowid}: '{old_name}' → base='{base_name}', grade='{grade}'")

            if migrated > 0:
                await self._db.commit()
                logger.info(f"[Migration] Successfully migrated {migrated} rows")
            else:
                logger.info("[Migration] No rows needed grade migration")
        except Exception as e:
            logger.error(f"[Migration] Item grade migration failed (will retry on next startup): {e}")

    async def _migrate_whitelist(self):
        """Migrate whitelist table from per-group to global if needed."""
        async with self._db.execute("PRAGMA table_info(whitelist)") as cursor:
            columns = [row[1] for row in await cursor.fetchall()]

        if "group_id" in columns:
            # Old schema detected: recreate without group_id, deduplicate
            await self._db.execute("""
                CREATE TABLE IF NOT EXISTS whitelist_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entry_type TEXT NOT NULL,
                    entry_id TEXT NOT NULL,
                    added_by TEXT,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(entry_type, entry_id)
                )
            """)
            await self._db.execute("""
                INSERT OR IGNORE INTO whitelist_new (entry_type, entry_id, added_by, added_at)
                SELECT DISTINCT entry_type, entry_id, added_by, added_at FROM whitelist
            """)
            await self._db.execute("DROP TABLE whitelist")
            await self._db.execute("ALTER TABLE whitelist_new RENAME TO whitelist")
            await self._db.execute("CREATE INDEX IF NOT EXISTS idx_whitelist_lookup ON whitelist(entry_type, entry_id)")
            await self._db.commit()

    def _row_to_player(self, row) -> Player:
        """Convert a database row tuple to a Player object."""
        return Player(
            player_id=row[0], group_id=row[1], player_name=row[2],
            class_=row[3], faith=row[4], ladder_score=row[5],
            pilgrimage_score=row[6], created_at=row[7], updated_at=row[8],
            oathbreaker=bool(row[9]) if len(row) > 9 else False
        )

    async def upsert_player(
        self, group_id: str, player_id: str, player_name: str,
        initial_ladder: int = 1000, initial_pilgrimage: int = 100
    ) -> Player:
        """Create or update a player record. New players get initial scores."""
        async with self._db.execute(
            "SELECT player_id, group_id, player_name, class, faith, ladder_score, pilgrimage_score, created_at, updated_at, oathbreaker FROM players WHERE player_id = ? AND group_id = ?",
            (player_id, group_id)
        ) as cursor:
            row = await cursor.fetchone()

        if row:
            # Update name if changed
            if row[2] != player_name:
                await self._db.execute(
                    "UPDATE players SET player_name = ?, updated_at = CURRENT_TIMESTAMP WHERE player_id = ? AND group_id = ?",
                    (player_name, player_id, group_id)
                )
                await self._db.commit()
                async with self._db.execute(
                    "SELECT player_id, group_id, player_name, class, faith, ladder_score, pilgrimage_score, created_at, updated_at, oathbreaker FROM players WHERE player_id = ? AND group_id = ?",
                    (player_id, group_id)
                ) as cursor:
                    updated_row = await cursor.fetchone()
                return self._row_to_player(updated_row)
            return self._row_to_player(row)
        else:
            # Create new player with initial scores
            await self._db.execute(
                "INSERT INTO players (player_id, group_id, player_name, ladder_score, pilgrimage_score) VALUES (?, ?, ?, ?, ?)",
                (player_id, group_id, player_name, initial_ladder, initial_pilgrimage)
            )
            await self._db.commit()
            return Player(
                player_id=player_id, group_id=group_id,
                player_name=player_name
            )

    async def get_player(self, group_id: str, player_id: str) -> Optional[Player]:
        """Get a player by ID and group."""
        async with self._db.execute(
            "SELECT player_id, group_id, player_name, class, faith, ladder_score, pilgrimage_score, created_at, updated_at, oathbreaker FROM players WHERE player_id = ? AND group_id = ?",
            (player_id, group_id)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return self._row_to_player(row)

    async def get_player_by_name(self, group_id: str, player_name: str) -> Optional[Player]:
        """Get a player by name and group (case-sensitive)."""
        async with self._db.execute(
            "SELECT player_id, group_id, player_name, class, faith, ladder_score, pilgrimage_score, created_at, updated_at, oathbreaker FROM players WHERE group_id = ? AND player_name = ?",
            (group_id, player_name)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return self._row_to_player(row)

    async def get_top_players(self, group_id: str, limit: int = 10) -> List[Player]:
        """Get top players by ladder score for a group."""
        async with self._db.execute(
            "SELECT player_id, group_id, player_name, class, faith, ladder_score, pilgrimage_score, created_at, updated_at, oathbreaker FROM players WHERE group_id = ? ORDER BY ladder_score DESC LIMIT ?",
            (group_id, limit)
        ) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_player(r) for r in rows]

    async def get_top_players_by_pilgrimage(self, group_id: str, limit: int = 10) -> List[Player]:
        """Get top players by pilgrimage score for a group."""
        async with self._db.execute(
            "SELECT player_id, group_id, player_name, class, faith, ladder_score, pilgrimage_score, created_at, updated_at, oathbreaker FROM players WHERE group_id = ? ORDER BY pilgrimage_score DESC LIMIT ?",
            (group_id, limit)
        ) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_player(r) for r in rows]

    async def get_player_ladder_rank(self, group_id: str, ladder_score: int, pilgrimage_score: int = 0) -> int:
        """Get a player's rank in the ladder (1-based).
        Tiebreaker: same ladder_score → higher pilgrimage_score ranks higher.
        """
        async with self._db.execute(
            "SELECT COUNT(*) + 1 FROM players WHERE group_id = ? "
            "AND (ladder_score > ? OR (ladder_score = ? AND pilgrimage_score > ?))",
            (group_id, ladder_score, ladder_score, pilgrimage_score)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 1

    async def get_player_pilgrimage_rank(self, group_id: str, pilgrimage_score: int, ladder_score: int = 0) -> int:
        """Get a player's rank in the pilgrimage ladder (1-based).
        Tiebreaker: same pilgrimage_score → higher ladder_score ranks higher.
        """
        async with self._db.execute(
            "SELECT COUNT(*) + 1 FROM players WHERE group_id = ? "
            "AND (pilgrimage_score > ? OR (pilgrimage_score = ? AND ladder_score > ?))",
            (group_id, pilgrimage_score, pilgrimage_score, ladder_score)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 1

    async def update_scores(
        self, group_id: str, player_id: str,
        ladder_delta: int, pilgrimage_delta: int,
        operator_id: str, reason: str = "",
        commit: bool = True
    ) -> Optional[Player]:
        """Update a player's scores and record history. Returns updated player or None if not found.

        Args:
            commit: If True (default), commits immediately. Set to False for batch operations
                    that should be committed atomically by the caller.
        """
        # Check player exists
        async with self._db.execute(
            "SELECT player_id FROM players WHERE player_id = ? AND group_id = ?",
            (player_id, group_id)
        ) as cursor:
            if not await cursor.fetchone():
                return None

        # Update scores
        await self._db.execute(
            "UPDATE players SET ladder_score = ladder_score + ?, pilgrimage_score = pilgrimage_score + ?, updated_at = CURRENT_TIMESTAMP WHERE player_id = ? AND group_id = ?",
            (ladder_delta, pilgrimage_delta, player_id, group_id)
        )

        # Record history
        await self._db.execute(
            "INSERT INTO score_history (player_id, group_id, ladder_change, pilgrimage_change, reason, operator_id) VALUES (?, ?, ?, ?, ?, ?)",
            (player_id, group_id, ladder_delta, pilgrimage_delta, reason, operator_id)
        )

        if commit:
            await self._db.commit()

        # Return updated player (same connection, no nested open)
        return await self.get_player(group_id, player_id)

    async def set_player_class(
        self, group_id: str, player_id: str,
        class_name: str, faith_name: str
    ) -> Optional[Player]:
        """Set a player's class and faith. Returns updated player or None if not found."""
        # Check player exists
        async with self._db.execute(
            "SELECT player_id FROM players WHERE player_id = ? AND group_id = ?",
            (player_id, group_id)
        ) as cursor:
            if not await cursor.fetchone():
                return None

        await self._db.execute(
            "UPDATE players SET class = ?, faith = ?, updated_at = CURRENT_TIMESTAMP WHERE player_id = ? AND group_id = ?",
            (class_name, faith_name, player_id, group_id)
        )
        await self._db.commit()
        return await self.get_player(group_id, player_id)

    async def set_player_faith(
        self, group_id: str, player_id: str, faith_name: str
    ) -> Optional[Player]:
        """Set a player's faith only. Returns updated player or None if not found."""
        async with self._db.execute(
            "SELECT player_id FROM players WHERE player_id = ? AND group_id = ?",
            (player_id, group_id)
        ) as cursor:
            if not await cursor.fetchone():
                return None

        await self._db.execute(
            "UPDATE players SET faith = ?, updated_at = CURRENT_TIMESTAMP WHERE player_id = ? AND group_id = ?",
            (faith_name, player_id, group_id)
        )
        await self._db.commit()
        return await self.get_player(group_id, player_id)

    async def set_oathbreaker(
        self, group_id: str, player_id: str, new_faith: Optional[str] = None
    ) -> Optional[Player]:
        """Mark a player as oathbreaker. Optionally update faith. Returns updated player or None."""
        async with self._db.execute(
            "SELECT player_id FROM players WHERE player_id = ? AND group_id = ?",
            (player_id, group_id)
        ) as cursor:
            if not await cursor.fetchone():
                return None

        if new_faith:
            await self._db.execute(
                "UPDATE players SET oathbreaker = 1, faith = ?, updated_at = CURRENT_TIMESTAMP WHERE player_id = ? AND group_id = ?",
                (new_faith, player_id, group_id)
            )
        else:
            await self._db.execute(
                "UPDATE players SET oathbreaker = 1, faith = NULL, updated_at = CURRENT_TIMESTAMP WHERE player_id = ? AND group_id = ?",
                (player_id, group_id)
            )
        await self._db.commit()
        return await self.get_player(group_id, player_id)

    async def clear_oathbreaker(self, group_id: str, player_id: str) -> Optional[Player]:
        """Clear a player's oathbreaker status. Returns updated player or None."""
        async with self._db.execute(
            "SELECT player_id FROM players WHERE player_id = ? AND group_id = ?",
            (player_id, group_id)
        ) as cursor:
            if not await cursor.fetchone():
                return None

        await self._db.execute(
            "UPDATE players SET oathbreaker = 0, updated_at = CURRENT_TIMESTAMP WHERE player_id = ? AND group_id = ?",
            (player_id, group_id)
        )
        await self._db.commit()
        return await self.get_player(group_id, player_id)

    async def delete_player(self, group_id: str, player_id: str) -> bool:
        """Delete a player and their score history. Returns True if deleted."""
        cursor = await self._db.execute(
            "DELETE FROM players WHERE player_id = ? AND group_id = ?",
            (player_id, group_id)
        )
        await self._db.execute(
            "DELETE FROM score_history WHERE player_id = ? AND group_id = ?",
            (player_id, group_id)
        )
        # Clean up player items
        await self._db.execute(
            "DELETE FROM player_items WHERE player_id = ? AND group_id = ?",
            (player_id, group_id)
        )
        # Clean up player statuses
        await self._db.execute(
            "DELETE FROM player_statuses WHERE player_id = ? AND group_id = ?",
            (player_id, group_id)
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def delete_player_by_name(self, group_id: str, player_name: str) -> bool:
        """Delete a player by name. Returns True if deleted."""
        # Single connection: look up then delete atomically
        async with self._db.execute(
            "SELECT player_id FROM players WHERE group_id = ? AND player_name = ?",
            (group_id, player_name)
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return False
        return await self.delete_player(group_id, row[0])

    async def rename_player_by_name(self, group_id: str, old_name: str, new_name: str) -> tuple[bool, str]:
        """Rename a player atomically. Returns (success, message).
        All checks and update happen on the same connection to prevent TOCTOU races."""
        # Find player by old name
        async with self._db.execute(
            "SELECT player_id FROM players WHERE group_id = ? AND player_name = ?",
            (group_id, old_name)
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return False, f"未找到玩家: {old_name}"

        # Check if new name already exists
        async with self._db.execute(
            "SELECT 1 FROM players WHERE group_id = ? AND player_name = ?",
            (group_id, new_name)
        ) as cursor:
            if await cursor.fetchone():
                return False, f"玩家名 {new_name} 已存在。"

        # Perform rename
        await self._db.execute(
            "UPDATE players SET player_name = ?, updated_at = CURRENT_TIMESTAMP WHERE player_id = ? AND group_id = ?",
            (new_name, row[0], group_id)
        )
        await self._db.commit()
        return True, f"已将玩家 {old_name} 改名为 {new_name}。"

    async def reset_all_scores(self, group_id: str, initial_ladder: int = 1000, initial_pilgrimage: int = 100) -> int:
        """Reset all players' scores to initial values. Returns number of players reset."""
        cursor = await self._db.execute(
            "UPDATE players SET ladder_score = ?, pilgrimage_score = ?, updated_at = CURRENT_TIMESTAMP WHERE group_id = ?",
            (initial_ladder, initial_pilgrimage, group_id)
        )
        await self._db.commit()
        return cursor.rowcount

    async def delete_all_players(self, group_id: str) -> int:
        """Delete all players, score history, and items in a group. Returns number of players deleted."""
        cursor = await self._db.execute(
            "DELETE FROM players WHERE group_id = ?", (group_id,)
        )
        await self._db.execute(
            "DELETE FROM score_history WHERE group_id = ?", (group_id,)
        )
        # Clean up all items in the group
        await self._db.execute(
            "DELETE FROM player_items WHERE group_id = ?", (group_id,)
        )
        # Clean up all statuses in the group
        await self._db.execute(
            "DELETE FROM player_statuses WHERE group_id = ?", (group_id,)
        )
        await self._db.commit()
        return cursor.rowcount

    # --- Global whitelist operations ---

    async def add_to_whitelist(
        self, entry_type: str, entry_id: str, added_by: str
    ) -> bool:
        """Add an entry to the global whitelist. Returns True if added, False if already exists."""
        try:
            await self._db.execute(
                "INSERT INTO whitelist (entry_type, entry_id, added_by) VALUES (?, ?, ?)",
                (entry_type, entry_id, added_by)
            )
            await self._db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def remove_from_whitelist(
        self, entry_type: str, entry_id: str
    ) -> bool:
        """Remove an entry from the global whitelist. Returns True if removed, False if not found."""
        cursor = await self._db.execute(
            "DELETE FROM whitelist WHERE entry_type = ? AND entry_id = ?",
            (entry_type, entry_id)
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def is_whitelisted(self, user_id: str) -> bool:
        """Check if a user is in the global whitelist."""
        async with self._db.execute(
            "SELECT 1 FROM whitelist WHERE entry_type = 'user' AND entry_id = ?",
            (user_id,)
        ) as cursor:
            return await cursor.fetchone() is not None

    async def get_whitelist(self) -> List[dict]:
        """Get all global whitelist entries."""
        async with self._db.execute(
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
        await self._db.execute(
            "INSERT OR REPLACE INTO active_groups (group_id, last_active) VALUES (?, CURRENT_TIMESTAMP)",
            (group_id,)
        )
        await self._db.commit()

    async def get_active_groups(self) -> List[str]:
        """Get all active group IDs."""
        async with self._db.execute("SELECT group_id FROM active_groups") as cursor:
            rows = await cursor.fetchall()
            return [r[0] for r in rows]

    # --- Group settings (per-group output mode) ---

    async def get_group_output_mode(self, group_id: str) -> Optional[str]:
        """Get the output mode override for a group. Returns None if not set (use global default)."""
        async with self._db.execute(
            "SELECT output_mode FROM group_settings WHERE group_id = ?",
            (group_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row and row[0] else None

    async def set_group_output_mode(self, group_id: str, mode: str):
        """Set the output mode for a group. Pass None or '' to clear (use global default)."""
        if mode in ("text", "image"):
            await self._db.execute(
                "INSERT OR REPLACE INTO group_settings (group_id, output_mode) VALUES (?, ?)",
                (group_id, mode)
            )
        else:
            # Clear override — fall back to global default
            await self._db.execute(
                "DELETE FROM group_settings WHERE group_id = ?",
                (group_id,)
            )
        await self._db.commit()

    # --- Score history retention ---

    async def purge_old_score_history(self, retention_days: int = 90) -> int:
        """Delete score history older than retention_days. Returns number of rows deleted.
        Note: SQLite CURRENT_TIMESTAMP is UTC, so we use UTC for the cutoff."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).strftime("%Y-%m-%d %H:%M:%S")
        cursor = await self._db.execute(
            "DELETE FROM score_history WHERE timestamp < ?",
            (cutoff,)
        )
        await self._db.commit()
        return cursor.rowcount

    # --- Backup ---

    async def backup_database(self, backup_dir: Path) -> Path:
        """Create a backup of the database using non-blocking I/O. Returns backup file path."""
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / f"ladder_backup_{timestamp}.db"
        await asyncio.to_thread(shutil.copy2, self.db_path, backup_path)
        return backup_path

    async def cleanup_old_backups(self, backup_dir: Path, retention_days: int):
        """Remove backups older than retention_days using non-blocking I/O."""
        if not backup_dir.exists():
            return

        cutoff = datetime.now().timestamp() - (retention_days * 86400)

        def _remove_old():
            for f in backup_dir.glob("ladder_backup_*.db"):
                if f.stat().st_mtime < cutoff:
                    f.unlink()

        await asyncio.to_thread(_remove_old)

    # === 道具（储物空间） ===

    async def add_item(self, group_id: str, player_id: str, item_name: str, quantity: int = 1, grade: str = None) -> None:
        """增加道具。item_name 为基础名，grade 为等级（可选）。已存在则累加数量。"""
        if quantity <= 0:
            return
        await self._db.execute(
            "INSERT INTO player_items (group_id, player_id, item_name, grade, quantity) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(group_id, player_id, item_name) DO UPDATE SET "
            "quantity = quantity + excluded.quantity, updated_at = CURRENT_TIMESTAMP",
            (group_id, player_id, item_name, grade, quantity)
        )

    async def remove_item(self, group_id: str, player_id: str, item_name: str, quantity: int = None, grade: str = None) -> bool:
        """减少道具。quantity=None 时全部删除。grade 不为 None 时精确匹配 grade。返回是否成功找到该道具。"""
        if grade is not None:
            where = "group_id = ? AND player_id = ? AND item_name = ? AND grade = ?"
            params = (group_id, player_id, item_name, grade)
        else:
            where = "group_id = ? AND player_id = ? AND item_name = ?"
            params = (group_id, player_id, item_name)

        if quantity is None:
            cursor = await self._db.execute(
                f"DELETE FROM player_items WHERE {where}", params
            )
            return cursor.rowcount > 0
        else:
            async with self._db.execute(
                f"SELECT quantity FROM player_items WHERE {where}", params
            ) as cursor:
                row = await cursor.fetchone()
            if not row:
                return False
            new_qty = row[0] - quantity
            if new_qty <= 0:
                await self._db.execute(
                    f"DELETE FROM player_items WHERE {where}", params
                )
            else:
                await self._db.execute(
                    f"UPDATE player_items SET quantity = ?, updated_at = CURRENT_TIMESTAMP "
                    f"WHERE {where}",
                    (new_qty,) + params
                )
            return True

    async def clear_items(self, group_id: str, player_id: str, item_name: str = None, grade: str = None) -> int:
        """清除道具。item_name=None → 清空全部；item_name 指定 → 清除该道具；+ grade → 指定等级。返回清除数量。"""
        if item_name is None:
            cursor = await self._db.execute(
                "DELETE FROM player_items WHERE group_id = ? AND player_id = ?",
                (group_id, player_id)
            )
        elif grade is None:
            cursor = await self._db.execute(
                "DELETE FROM player_items WHERE group_id = ? AND player_id = ? AND item_name = ?",
                (group_id, player_id, item_name)
            )
        else:
            cursor = await self._db.execute(
                "DELETE FROM player_items WHERE group_id = ? AND player_id = ? AND item_name = ? AND grade = ?",
                (group_id, player_id, item_name, grade)
            )
        await self._db.commit()
        return cursor.rowcount

    async def get_player_items(self, group_id: str, player_id: str) -> list:
        """获取玩家所有道具。返回 [{"item_name": str, "grade": str|None, "quantity": int}, ...]
        按等级从高到低排序：SSS > SS > S > A > B > C > 无等级。"""
        grade_order = {"SSS": 0, "SS": 1, "S": 2, "A": 3, "B": 4, "C": 5}
        async with self._db.execute(
            "SELECT item_name, grade, quantity FROM player_items "
            "WHERE group_id = ? AND player_id = ?",
            (group_id, player_id)
        ) as cursor:
            rows = await cursor.fetchall()
        results = [{"item_name": r[0], "grade": r[1], "quantity": r[2]} for r in rows]
        results.sort(key=lambda x: (grade_order.get(x["grade"], 99) if x["grade"] else 100))
        return results

    async def delete_all_items(self, group_id: str, player_id: str) -> int:
        """清空玩家所有道具。返回删除的道具种类数。"""
        cursor = await self._db.execute(
            "DELETE FROM player_items WHERE group_id = ? AND player_id = ?",
            (group_id, player_id)
        )
        return cursor.rowcount

    # === 状态 ===

    async def add_status(self, group_id: str, player_id: str, status_name: str, days: int) -> None:
        """添加状态。从当前时间开始持续 days 天。"""
        if days <= 0:
            return
        from datetime import datetime, timedelta
        expire_at = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        await self._db.execute(
            "INSERT INTO player_statuses (group_id, player_id, status_name, expire_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(group_id, player_id, status_name) DO UPDATE SET "
            "expire_at = excluded.expire_at",
            (group_id, player_id, status_name, expire_at)
        )

    async def remove_status(self, group_id: str, player_id: str, status_name: str) -> bool:
        """移除指定状态。返回是否成功找到。"""
        cursor = await self._db.execute(
            "DELETE FROM player_statuses WHERE group_id = ? AND player_id = ? AND status_name = ?",
            (group_id, player_id, status_name)
        )
        return cursor.rowcount > 0

    async def clear_statuses(self, group_id: str, player_id: str) -> int:
        """清除玩家所有状态。返回删除数量。"""
        cursor = await self._db.execute(
            "DELETE FROM player_statuses WHERE group_id = ? AND player_id = ?",
            (group_id, player_id)
        )
        return cursor.rowcount

    async def get_player_statuses(self, group_id: str, player_id: str) -> list:
        """获取玩家未过期的状态列表。返回 [{"status_name": str, "expire_at": str, "remaining_days": int}, ...]"""
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        async with self._db.execute(
            "SELECT status_name, expire_at FROM player_statuses "
            "WHERE group_id = ? AND player_id = ? AND expire_at > ? "
            "ORDER BY expire_at",
            (group_id, player_id, now)
        ) as cursor:
            rows = await cursor.fetchall()
            result = []
            now_dt = datetime.now()
            for r in rows:
                expire_dt = datetime.strptime(r[1], "%Y-%m-%d %H:%M:%S")
                delta = expire_dt - now_dt
                # 按日历天计算：向上取整（不足一天算一天）
                remaining = max(0, delta.days + (1 if delta.seconds > 0 else 0))
                result.append({
                    "status_name": r[0],
                    "expire_at": r[1],
                    "remaining_days": remaining,
                })
            return result

    async def purge_expired_statuses(self) -> int:
        """清理所有过期状态记录。返回删除数量。"""
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor = await self._db.execute(
            "DELETE FROM player_statuses WHERE expire_at <= ?",
            (now,)
        )
        return cursor.rowcount

    async def close(self):
        """Close the persistent database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    async def commit(self):
        """Commit the current transaction. Exposed for multi-step atomic operations."""
        if self._db:
            await self._db.commit()
