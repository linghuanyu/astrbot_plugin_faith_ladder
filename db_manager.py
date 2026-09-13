"""
数据库访问层。用 aiosqlite 单连接持有 ladder.db，负责建表、迁移与全部 SQL 操作。

等级字段的编码说明见 item_utils.grade_to_storage：解析侧是三态（None/''/等级），
存储侧是 NOT NULL 的文本（''/'-'/等级），因为 grade 参与 player_items 主键。
"""

import aiosqlite
from pathlib import Path
from typing import List, Optional, Tuple
from datetime import datetime, timedelta, timezone

from astrbot_plugin_faith_ladder.item_utils import (
    grade_to_storage,
    grade_from_storage,
    GRADE_STORAGE_NONE,
)

# 北京时间 UTC+8（仅用于「每日」口径：祷词/赠送次数按北京日期分界；
# 其余时间戳统一用 UTC，见 add_status / purge_old_score_history）
BEIJING_TZ = timezone(timedelta(hours=8))

from astrbot_plugin_faith_ladder.models import Player, FAITH_TO_PATH

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)


class DatabaseManager:
    """Manages all database operations for the faith ladder plugin."""

    # Column list for players SELECT queries (kept in one place so schema changes
    # only need to be updated here + in _row_to_player).
    _PLAYER_COLUMNS = (
        "player_id, group_id, player_name, class, faith, specific_faith, "
        "ladder_score, pilgrimage_score, created_at, updated_at, oathbreaker, qq_id"
    )

    def __init__(self, data_dir: Path):
        """只记录数据目录与库文件路径；建表、迁移与连接建立都在 initialize() 里做。"""
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
                specific_faith TEXT DEFAULT NULL,
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

            CREATE TABLE IF NOT EXISTS player_items (
                group_id TEXT NOT NULL,
                player_id TEXT NOT NULL,
                item_name TEXT NOT NULL,
                grade TEXT NOT NULL DEFAULT '',
                quantity INTEGER DEFAULT 1,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (group_id, player_id, item_name, grade)
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

            CREATE TABLE IF NOT EXISTS pending_gifts (
                group_id TEXT NOT NULL,
                receiver_id TEXT NOT NULL,
                sender_id TEXT NOT NULL,
                sender_name TEXT NOT NULL,
                receiver_name TEXT NOT NULL,
                items_json TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (group_id, receiver_id)
            );

            CREATE TABLE IF NOT EXISTS prayer_daily_hits (
                group_id TEXT NOT NULL,
                player_id TEXT NOT NULL,
                hit_date TEXT NOT NULL,
                delta INTEGER NOT NULL,
                PRIMARY KEY (group_id, player_id, hit_date)
            );

            CREATE TABLE IF NOT EXISTS gift_daily_accepts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                receiver_id TEXT NOT NULL,
                accept_date TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_gift_accepts_lookup
                ON gift_daily_accepts(group_id, receiver_id, accept_date);
        """)
        await self._db.commit()

        # Migrate old whitelist table (had group_id column) to global whitelist
        await self._migrate_whitelist()

        # Migrate: add oathbreaker column if missing
        await self._migrate_oathbreaker()

        # Migrate: clean up old item names with *N suffix
        await self._migrate_item_names()

        # Migrate: add grade column to player_items
        await self.migrate_player_items()

        # Migrate: rebuild player_items so the PK includes grade
        await self._migrate_items_grade_pk()

        # Migrate: add qq_id column to players (QQ binding for anti-impersonation)
        await self._migrate_qq_id()

        # Migrate: add specific_faith column to players
        await self._migrate_specific_faith()

        # Migrate: remove deprecated group entries from whitelist
        await self._migrate_whitelist_remove_groups()

        # Migrate: add faith column to whitelist
        await self._migrate_whitelist_faith()

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
        """清理旧数据里带 `*N` 后缀的道具名（如 '糖果*3' → '糖果'，数量乘 N）。

        判定条件放宽为「名字以 *数字 结尾」：旧解析器会把数量写进名字，
        无论前面是否带品级括号（'糖果*3'、'护身符（C级）*2'、
        以及多重后缀 '糖果*3*1' 都是同一类脏数据）。
        此前只处理带括号的两种形式，文档里承诺的 '糖果*3' 反而漏掉了，
        这类行永远不会被清理、也无法与正常同名道具合并。
        """
        import re
        async with self._db.execute(
            "SELECT group_id, player_id, item_name, quantity FROM player_items"
        ) as cursor:
            rows = await cursor.fetchall()

        for group_id, player_id, item_name, quantity in rows:
            # 只要名字以 *数字 结尾就是旧解析器留下的脏数据（正常名字不含 *N）
            if not re.search(r'\*\d+$', item_name):
                continue

            # 剥离所有尾部 *N，并把数量按乘积折算回去
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

    async def migrate_player_items(self) -> int:
        """把旧数据里写在道具名中的等级拆到 grade 列，并清理重复行。

        '共生噬刃（C级）' → item_name='共生噬刃', grade='C'；目标行已存在时合并数量。
        幂等：启动时自动执行，也可用「天梯榜管理 迁移储物空间」手动重跑。

        目标行的匹配必须带上 grade（存储形态），否则带等级的道具会被合并进
        同名无等级那一行、等级直接丢失（这正是本次修复的问题之一）。
        返回处理过的行数。
        """
        from astrbot_plugin_faith_ladder.item_utils import parse_item_full_name, grade_to_storage

        async with self._db.execute("PRAGMA table_info(player_items)") as cursor:
            columns = [row[1] for row in await cursor.fetchall()]
        if "grade" not in columns:
            await self._db.execute("ALTER TABLE player_items ADD COLUMN grade TEXT NOT NULL DEFAULT ''")
            logger.info("[Migration] Added 'grade' column to player_items")

        try:
            # 每次启动全表扫一遍，容忍上次中途失败
            async with self._db.execute(
                "SELECT rowid, group_id, player_id, item_name, quantity FROM player_items"
            ) as cursor:
                rows = await cursor.fetchall()

            migrated = 0
            merged = 0
            for rowid, group_id, player_id, old_name, quantity in rows:
                base_name, parsed_grade = parse_item_full_name(old_name)
                if base_name == old_name:
                    continue  # 名字里本来就没有等级括号

                target_grade = grade_to_storage(parsed_grade)
                async with self._db.execute(
                    "SELECT rowid, quantity FROM player_items "
                    "WHERE group_id = ? AND player_id = ? AND item_name = ? AND grade = ?",
                    (group_id, player_id, base_name, target_grade)
                ) as check_cursor:
                    existing = await check_cursor.fetchone()

                if existing:
                    existing_rowid, existing_qty = existing
                    await self._db.execute(
                        "UPDATE player_items SET quantity = ?, updated_at = CURRENT_TIMESTAMP WHERE rowid = ?",
                        (existing_qty + quantity, existing_rowid)
                    )
                    await self._db.execute("DELETE FROM player_items WHERE rowid = ?", (rowid,))
                    merged += 1
                    logger.info(
                        f"[Migration] Merged row {rowid} into {existing_rowid}: "
                        f"'{old_name}' → '{base_name}' (qty {quantity}+{existing_qty})"
                    )
                else:
                    await self._db.execute(
                        "UPDATE player_items SET grade = ?, item_name = ? WHERE rowid = ?",
                        (target_grade, base_name, rowid)
                    )
                    migrated += 1
                    logger.info(f"[Migration] Row {rowid}: '{old_name}' → base='{base_name}', grade='{target_grade}'")

            if migrated or merged:
                await self._db.commit()
                logger.info(f"[Migration] 道具等级迁移：改写 {migrated} 行，合并 {merged} 行")
            return migrated + merged
        except Exception as e:
            await self._db.rollback()
            logger.error(f"[Migration] 道具等级迁移失败（下次启动会重试）: {e}")
            return 0

    async def _migrate_items_grade_pk(self):
        """重建 player_items，使主键包含 grade（(group, player, name, grade)）。

        旧主键下一个玩家无法同时持有「铁剑」和「铁剑（A级）」：后者会被合并进前者
        并丢掉等级，之后按等级赠送/扣除都会失败。SQLite 不能直接改主键，只能重建表。

        grade 同时归一化为 NOT NULL：NULL 在 UNIQUE/主键约束下互不相等，
        若允许 NULL，"无等级"道具会被反复插入成多行而不是合并。
        （无等级 → ''，有括号但非标准等级 → '-'，见 item_utils.grade_to_storage）
        通过检查现有主键列判断是否已迁移，可重复执行。
        """
        async with self._db.execute("PRAGMA table_info(player_items)") as cursor:
            info = await cursor.fetchall()
        pk_cols = [r[1] for r in info if r[5]]
        if "grade" in pk_cols:
            return

        logger.info(f"[Migration] 重建 player_items 主键：{pk_cols} → 加入 grade")
        try:
            await self._db.executescript("""
                CREATE TABLE IF NOT EXISTS player_items_new (
                    group_id TEXT NOT NULL,
                    player_id TEXT NOT NULL,
                    item_name TEXT NOT NULL,
                    grade TEXT NOT NULL DEFAULT '',
                    quantity INTEGER DEFAULT 1,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (group_id, player_id, item_name, grade)
                );
                INSERT INTO player_items_new (group_id, player_id, item_name, grade, quantity, updated_at)
                    SELECT group_id, player_id, item_name,
                           CASE WHEN grade IS NULL THEN '' ELSE grade END,
                           quantity, updated_at
                    FROM player_items;
                DROP TABLE player_items;
                ALTER TABLE player_items_new RENAME TO player_items;
                CREATE INDEX IF NOT EXISTS idx_player_items_lookup ON player_items(group_id, player_id);
            """)
            await self._db.commit()
            logger.info("[Migration] player_items 重建完成")
        except Exception as e:
            await self._db.rollback()
            logger.error(f"[Migration] player_items 主键重建失败（下次启动会重试）: {e}")

    async def _migrate_qq_id(self):
        """Add qq_id column + unique-per-group index to players table.
        SQLite UNIQUE allows multiple NULLs, so unbound rows don't violate the index.
        """
        async with self._db.execute("PRAGMA table_info(players)") as cursor:
            columns = [row[1] for row in await cursor.fetchall()]

        if "qq_id" not in columns:
            await self._db.execute("ALTER TABLE players ADD COLUMN qq_id TEXT")
            await self._db.commit()
            logger.info("[Migration] Added qq_id column to players")

        # 索引创建放在 if 之外：曾经出现过「列已加、索引没建成」的半应用状态
        # （迁移中途失败、或由更早的版本建库），那种库会永久失去 QQ 唯一性约束
        await self._db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_players_qq "
            "ON players(group_id, qq_id)"
        )
        await self._db.commit()

    async def _migrate_specific_faith(self):
        """Add specific_faith column to players table if it doesn't exist."""
        async with self._db.execute("PRAGMA table_info(players)") as cursor:
            columns = [row[1] for row in await cursor.fetchall()]

        if "specific_faith" not in columns:
            await self._db.execute(
                "ALTER TABLE players ADD COLUMN specific_faith TEXT DEFAULT NULL"
            )
            await self._db.commit()
            logger.info("[Migration] Added specific_faith column to players")

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

    async def _migrate_whitelist_remove_groups(self):
        """移除白名单中已废弃的 group 类型条目。"""
        cursor = await self._db.execute("DELETE FROM whitelist WHERE entry_type = 'group'")
        if cursor.rowcount > 0:
            logger.info(f"[Migration] 移除 {cursor.rowcount} 条已废弃的 group 白名单")
        await self._db.commit()

    async def _migrate_whitelist_faith(self):
        """白名单新增 faith 字段（诸神对应信仰）。"""
        async with self._db.execute("PRAGMA table_info(whitelist)") as cursor:
            columns = [row[1] for row in await cursor.fetchall()]
        if "faith" not in columns:
            await self._db.execute("ALTER TABLE whitelist ADD COLUMN faith TEXT DEFAULT NULL")
            await self._db.commit()
            logger.info("[Migration] Added 'faith' column to whitelist")

    async def get_whitelist_faith(self, entry_id: str) -> Optional[str]:
        """获取白名单条目对应的信仰名。"""
        async with self._db.execute(
            "SELECT faith FROM whitelist WHERE entry_id = ? AND entry_type = 'user'",
            (entry_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

    async def set_whitelist_faith(self, entry_id: str, faith: str) -> bool:
        """设置白名单条目的信仰。返回是否成功。"""
        cursor = await self._db.execute(
            "UPDATE whitelist SET faith = ? WHERE entry_id = ? AND entry_type = 'user'",
            (faith, entry_id)
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def get_whitelist_with_faith(self) -> List[dict]:
        """获取白名单列表，包含信仰字段。"""
        async with self._db.execute(
            "SELECT entry_type, entry_id, faith, added_by, added_at FROM whitelist ORDER BY id"
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                {
                    "entry_type": r[0],
                    "entry_id": r[1],
                    "faith": r[2],
                    "added_by": r[3],
                    "added_at": r[4],
                }
                for r in rows
            ]

    def _row_to_player(self, row) -> Player:
        """Convert a database row tuple to a Player object."""
        return Player(
            player_id=row[0], group_id=row[1], player_name=row[2],
            class_=row[3], faith=row[4], specific_faith=row[5],
            ladder_score=row[6], pilgrimage_score=row[7],
            created_at=row[8], updated_at=row[9],
            oathbreaker=bool(row[10]) if len(row) > 10 else False,
            qq_id=row[11] if len(row) > 11 else None,
        )

    async def upsert_player(
        self, group_id: str, player_id: str, player_name: str,
        initial_ladder: int = 1000, initial_pilgrimage: int = 100,
        commit: bool = True,
    ) -> Player:
        """创建或更新玩家记录。新玩家使用初始分；已存在时仅在名字变化时改名。

        用单条 INSERT ... ON CONFLICT 完成「先查后插」：旧的 SELECT-then-INSERT
        在并发下会让两个协程都查不到记录，随后第二个 INSERT 撞主键抛
        IntegrityError（例如两人同时录入同一玩家名）。
        commit=False 供需要多步原子写入的调用方使用（见 register_player）。
        """
        await self._db.execute(
            "INSERT INTO players (player_id, group_id, player_name, ladder_score, pilgrimage_score) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(player_id, group_id) DO UPDATE SET "
            "player_name = excluded.player_name, updated_at = CURRENT_TIMESTAMP "
            "WHERE players.player_name <> excluded.player_name",
            (player_id, group_id, player_name, initial_ladder, initial_pilgrimage)
        )
        if commit:
            await self._db.commit()
        # 刚刚 upsert 过，此处必然存在
        return await self.get_player(group_id, player_id)

    async def get_player(self, group_id: str, player_id: str) -> Optional[Player]:
        """Get a player by ID and group."""
        async with self._db.execute(
            "SELECT player_id, group_id, player_name, class, faith, specific_faith, ladder_score, pilgrimage_score, created_at, updated_at, oathbreaker, qq_id FROM players WHERE player_id = ? AND group_id = ?",
            (player_id, group_id)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return self._row_to_player(row)

    async def get_player_by_name(self, group_id: str, player_name: str) -> Optional[Player]:
        """Get a player by name and group (case-sensitive)."""
        async with self._db.execute(
            f"SELECT {self._PLAYER_COLUMNS} FROM players WHERE group_id = ? AND player_name = ?",
            (group_id, player_name)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return self._row_to_player(row)

    async def get_players_by_names(self, group_id: str, player_names: List[str]) -> dict:
        """批量查询玩家。返回 {player_name: Player} 字典，不存在的玩家不包含在内。"""
        if not player_names:
            return {}
        placeholders = ','.join('?' * len(player_names))
        query = f"SELECT {self._PLAYER_COLUMNS} FROM players WHERE group_id = ? AND player_name IN ({placeholders})"
        params = [group_id] + player_names
        result = {}
        async with self._db.execute(query, params) as cursor:
            async for row in cursor:
                player = self._row_to_player(row)
                result[player.player_name] = player
        return result

    async def get_player_by_qq(self, group_id: str, qq_id: str) -> Optional[Player]:
        """Get a player by bound QQ ID and group. Returns None if no binding."""
        async with self._db.execute(
            f"SELECT {self._PLAYER_COLUMNS} FROM players WHERE group_id = ? AND qq_id = ?",
            (group_id, str(qq_id))
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            return self._row_to_player(row)

    async def set_player_qq(self, group_id: str, player_id: str, qq_id: str, commit: bool = True) -> bool:
        """Bind a QQ ID to a player. Returns True on success, False on unique conflict.

        并发下两个绑定请求可能同时通过上面的检查，因此这里额外捕获唯一索引
        （idx_players_qq）冲突并返回 False，而不是让 IntegrityError 冒到调用方。
        commit=False 供多步原子写入的调用方使用（见 register_player）。
        """
        qq_id = str(qq_id)
        # Check existing binding for this QQ (same or different player)
        async with self._db.execute(
            "SELECT player_id FROM players WHERE group_id = ? AND qq_id = ?",
            (group_id, qq_id)
        ) as cursor:
            row = await cursor.fetchone()
            if row and row[0] != player_id:
                return False  # QQ already bound to another player in this group
        try:
            cursor = await self._db.execute(
                "UPDATE players SET qq_id = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE group_id = ? AND player_id = ?",
                (qq_id, group_id, player_id)
            )
        except aiosqlite.IntegrityError:
            await self.rollback()
            return False
        if commit:
            await self._db.commit()
        # rowcount 为 0 表示玩家不存在，绑定并未真正发生
        return cursor.rowcount > 0

    async def rebind_player_qq(
        self, group_id: str, player_id: str, new_qq: str
    ) -> Tuple[bool, str, Optional[str]]:
        """换绑玩家 QQ：先清除该玩家旧绑定，再绑定到新 QQ。
        返回 (success, message, old_qq)。若 new_qq 已被其他玩家占用，返回冲突错误。
        """
        new_qq = str(new_qq)
        # 1) 查旧绑定
        async with self._db.execute(
            "SELECT qq_id FROM players WHERE group_id = ? AND player_id = ?",
            (group_id, player_id)
        ) as cursor:
            row = await cursor.fetchone()
            old_qq = row[0] if row else None

        # 2) 若新 QQ 已被其他玩家占用 → 拒绝
        async with self._db.execute(
            "SELECT player_id, player_name FROM players WHERE group_id = ? AND qq_id = ?",
            (group_id, new_qq)
        ) as cursor:
            conflict = await cursor.fetchone()
            if conflict and conflict[0] != player_id:
                return False, f"QQ {new_qq} 已被玩家 {conflict[1]} 绑定，请先让其换绑或解绑。", old_qq

        # 3) 更新为新 QQ
        await self._db.execute(
            "UPDATE players SET qq_id = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE group_id = ? AND player_id = ?",
            (new_qq, group_id, player_id)
        )
        await self._db.commit()
        return True, "换绑成功", old_qq

    async def get_top_players(self, group_id: str, limit: int = 10) -> List[Player]:
        """Get top players by ladder score for a group."""
        async with self._db.execute(
            "SELECT player_id, group_id, player_name, class, faith, specific_faith, ladder_score, pilgrimage_score, created_at, updated_at, oathbreaker, qq_id FROM players WHERE group_id = ? ORDER BY ladder_score DESC LIMIT ?",
            (group_id, limit)
        ) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_player(r) for r in rows]

    async def get_top_players_by_pilgrimage(self, group_id: str, limit: int = 10) -> List[Player]:
        """Get top players by pilgrimage score for a group."""
        async with self._db.execute(
            "SELECT player_id, group_id, player_name, class, faith, specific_faith, ladder_score, pilgrimage_score, created_at, updated_at, oathbreaker, qq_id FROM players WHERE group_id = ? ORDER BY pilgrimage_score DESC LIMIT ?",
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
        class_name: str, faith_name: str, commit: bool = True,
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
        if commit:
            await self._db.commit()
        return await self.get_player(group_id, player_id)

    async def set_player_faith(
        self, group_id: str, player_id: str, faith_name: str
    ) -> Optional[Player]:
        """Set a player's faith (path) only. Returns updated player or None if not found."""
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

    async def set_player_specific_faith(
        self, group_id: str, player_id: str, specific_faith: str, commit: bool = True
    ) -> Optional[Player]:
        """设置玩家的具体信仰（如"繁荣"），并同步推导出的命途。

        命途由具体信仰映射而来；无法识别的信仰只写具体信仰、不动原有命途
        （否则 FAITH_TO_PATH.get 返回 None 会把已有命途清成 NULL）。
        commit=False 供需要多步原子写入的调用方使用（见 register_player）。
        """
        async with self._db.execute(
            "SELECT player_id FROM players WHERE player_id = ? AND group_id = ?",
            (player_id, group_id)
        ) as cursor:
            if not await cursor.fetchone():
                return None

        path = FAITH_TO_PATH.get(specific_faith)
        if path:
            await self._db.execute(
                "UPDATE players SET specific_faith = ?, faith = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE player_id = ? AND group_id = ?",
                (specific_faith, path, player_id, group_id)
            )
        else:
            await self._db.execute(
                "UPDATE players SET specific_faith = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE player_id = ? AND group_id = ?",
                (specific_faith, player_id, group_id)
            )
        if commit:
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
        """Delete a player and everything attached to them. Returns True if deleted.

        除玩家本体外还要清掉依赖玩家 ID 的附属数据：道具、状态、积分历史，以及
        pending_gifts / prayer_daily_hits / gift_daily_accepts。后三张表若残留，
        同名玩家重新录入后会继承旧的待领取赠送、当日祷词记录与接受配额。
        """
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
        # 待处理赠送：作为接收方（该笔已无意义）或作为发送方（退款会打到已删除的玩家）都要清
        await self._db.execute(
            "DELETE FROM pending_gifts WHERE group_id = ? AND (receiver_id = ? OR sender_id = ?)",
            (group_id, player_id, player_id)
        )
        # Clean up daily-state records
        await self._db.execute(
            "DELETE FROM prayer_daily_hits WHERE group_id = ? AND player_id = ?",
            (group_id, player_id)
        )
        await self._db.execute(
            "DELETE FROM gift_daily_accepts WHERE group_id = ? AND receiver_id = ?",
            (group_id, player_id)
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
        """Rename a player. Returns (success, message).

        players.player_name 上并没有唯一约束，所以「先查重名、再 UPDATE」在并发下
        会让两个改名请求同时通过检查、产生两个同名玩家（之后按名字查找的结果随机）。
        这里把重名判断并进 UPDATE 语句本身，用 rowcount 判定是否真的改到了。
        """
        # Find player by old name
        async with self._db.execute(
            "SELECT player_id FROM players WHERE group_id = ? AND player_name = ?",
            (group_id, old_name)
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return False, f"未找到玩家: {old_name}"

        # 改名与去重在同一条语句里完成：目标名已被占用时 EXISTS 为真，UPDATE 不命中任何行
        cursor = await self._db.execute(
            "UPDATE players SET player_name = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE group_id = ? AND player_id = ? "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM players p2 "
            "  WHERE p2.group_id = ? AND p2.player_name = ? AND p2.player_id <> ?"
            ")",
            (new_name, group_id, row[0], group_id, new_name, row[0])
        )
        if cursor.rowcount <= 0:
            await self._db.rollback()
            return False, f"玩家名 {new_name} 已存在。"
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
        """Delete all players and their attached data in a group. Returns number of players deleted.

        与 delete_player 对应：同样要清掉 pending_gifts / prayer_daily_hits /
        gift_daily_accepts，否则清空之后再录入同名玩家会继承旧状态。
        """
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
        # Clean up all daily-state / pending-gift rows of the group
        await self._db.execute(
            "DELETE FROM pending_gifts WHERE group_id = ?", (group_id,)
        )
        await self._db.execute(
            "DELETE FROM prayer_daily_hits WHERE group_id = ?", (group_id,)
        )
        await self._db.execute(
            "DELETE FROM gift_daily_accepts WHERE group_id = ?", (group_id,)
        )
        await self._db.commit()
        return cursor.rowcount

    # --- Global whitelist operations ---

    async def add_to_whitelist(
        self, entry_type: str, entry_id: str, added_by: str, faith: str = None
    ) -> bool:
        """Add an entry to the global whitelist. Returns True if added, False if already exists."""
        try:
            await self._db.execute(
                "INSERT INTO whitelist (entry_type, entry_id, added_by, faith) VALUES (?, ?, ?, ?)",
                (entry_type, entry_id, added_by, faith)
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
        """Get all global whitelist entries. 仅返回 user 类型（group 类型已废弃）。"""
        async with self._db.execute(
            "SELECT entry_type, entry_id, faith, added_by, added_at FROM whitelist WHERE entry_type = 'user'"
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                {
                    "entry_type": r[0],
                    "entry_id": r[1],
                    "faith": r[2],
                    "added_by": r[3],
                    "added_at": r[4]
                }
                for r in rows
            ]

    # --- Active groups ---

    # --- Backup ---

    async def backup_to(self, backup_path: Path) -> None:
        """用 SQLite 的 VACUUM INTO 生成一致性备份到 backup_path。

        此前是 shutil.copy2 直接拷 .db 文件：单连接下随时可能有未提交的写入，
        而且不会一起拷 -journal，备份可能撕裂或包含已被回滚的数据。
        VACUUM INTO 由 SQLite 自己产出快照，结果自洽。
        目标文件必须不存在（SQLite 的限制），故先清掉同名残留。
        """
        if self._db is None:
            raise RuntimeError("数据库尚未初始化，无法备份")
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        if backup_path.exists():
            backup_path.unlink()
        await self._db.execute("VACUUM INTO ?", (str(backup_path),))

    # === 道具（储物空间） ===

    async def add_item(self, group_id: str, player_id: str, item_name: str, quantity: int = 1, grade: str = None) -> None:
        """增加道具。item_name 为基础名，grade 为解析侧三态等级（None / '' / 有效等级）。

        同名不同等级是各自独立的行（主键含 grade）：给已持有无等级「铁剑」的玩家
        赐予「铁剑（A级）」会新增一行，而不是把等级并进旧行后丢掉。
        """
        if quantity <= 0:
            return
        await self._db.execute(
            "INSERT INTO player_items (group_id, player_id, item_name, grade, quantity) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(group_id, player_id, item_name, grade) DO UPDATE SET "
            "quantity = quantity + excluded.quantity, updated_at = CURRENT_TIMESTAMP",
            (group_id, player_id, item_name, grade_to_storage(grade), quantity)
        )

    async def remove_item(self, group_id: str, player_id: str, item_name: str, quantity: int = None,
                          grade: str = None, require_sufficient: bool = False) -> bool:
        """减少道具。quantity=None 时删除匹配到的行。grade 为解析侧三态等级。

        匹配范围：
        - 指定 grade → 只作用于该等级那一行
        - grade=None 且 quantity=None（"全部清除"）→ 删除该名字下的所有等级行（保持旧语义）
        - grade=None 且有 quantity（"扣若干数量"）→ 只作用于"无等级"那一行

        require_sufficient=False（默认，保持旧语义）：数量不足时截断到 0，只要命中行就返回 True。
        require_sufficient=True：数量不足时不改动数据并返回 False，供并发下"扣到才算成功"的场景。
        返回是否成功扣减（没有命中任何行时为 False）。
        """
        base_where = "group_id = ? AND player_id = ? AND item_name = ?"
        base_params = (group_id, player_id, item_name)
        if grade is not None:
            where = base_where + " AND grade = ?"
            params = base_params + (grade_to_storage(grade),)
        elif quantity is None:
            where = base_where
            params = base_params
        else:
            where = base_where + " AND grade = ?"
            params = base_params + (GRADE_STORAGE_NONE,)

        if quantity is None:
            cursor = await self._db.execute(
                f"DELETE FROM player_items WHERE {where}", params
            )
            return cursor.rowcount > 0

        if require_sufficient:
            # 单条带守卫的原子扣减：并发下只有一个调用能扣到，扣不到的完全不改数据
            cursor = await self._db.execute(
                f"UPDATE player_items SET quantity = quantity - ?, updated_at = CURRENT_TIMESTAMP "
                f"WHERE {where} AND quantity >= ?",
                (quantity,) + params + (quantity,)
            )
        else:
            # 单条语句内完成扣减与截断：并发下不会互相覆盖数量，也不会扣成负数
            cursor = await self._db.execute(
                f"UPDATE player_items SET quantity = MAX(quantity - ?, 0), updated_at = CURRENT_TIMESTAMP "
                f"WHERE {where}",
                (quantity,) + params
            )
        if cursor.rowcount <= 0:
            return False
        # 扣到 0 的行直接移除，保持与旧行为一致
        await self._db.execute(
            f"DELETE FROM player_items WHERE {where} AND quantity <= 0", params
        )
        return True

    async def clear_items(self, group_id: str, player_id: str, item_name: str = None, grade: str = None) -> int:
        """清除道具。item_name=None → 清空全部；指定名字 → 清除该名字（含各等级）；+ grade → 只清该等级。"""
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
                (group_id, player_id, item_name, grade_to_storage(grade))
            )
        await self._db.commit()
        return cursor.rowcount

    async def get_player_items(self, group_id: str, player_id: str) -> list:
        """获取玩家所有道具。返回 [{"item_name": str, "grade": str|None, "quantity": int}, ...]

        grade 以解析侧三态返回（None=无等级括号，''=有括号但非标准等级，'C' 等=有效等级），
        调用方无需关心存储哨兵。按等级从高到低排序：SSS > SS > S > A > B > C > 无等级。
        """
        grade_order = {"SSS": 0, "SS": 1, "S": 2, "A": 3, "B": 4, "C": 5}
        async with self._db.execute(
            "SELECT item_name, grade, quantity FROM player_items "
            "WHERE group_id = ? AND player_id = ?",
            (group_id, player_id)
        ) as cursor:
            rows = await cursor.fetchall()
        results = [
            {"item_name": r[0], "grade": grade_from_storage(r[1]), "quantity": r[2]}
            for r in rows
        ]
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
        """添加状态。从当前时间开始持续 days 天。

        expire_at 以 UTC 存储（与 score_history / CURRENT_TIMESTAMP 一致），
        因此所有比较与到期判定都必须用 UTC；调用方负责 commit。
        """
        if days <= 0:
            return
        from datetime import datetime, timedelta, timezone
        expire_at = (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
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

    async def get_all_players_in_group(self, group_id: str) -> List[Player]:
        """获取群内所有玩家（用于批量计算排名）。"""
        async with self._db.execute(
            "SELECT player_id, group_id, player_name, class, faith, specific_faith, "
            "ladder_score, pilgrimage_score, created_at, updated_at, oathbreaker, qq_id "
            "FROM players WHERE group_id = ?",
            (group_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [self._row_to_player(r) for r in rows]

    async def get_statuses_for_players(self, group_id: str, player_ids: List[str]) -> list:
        """批量获取多个玩家的状态。返回 [(player_id, [statuses]), ...]。"""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        placeholders = ','.join('?' * len(player_ids))
        query = (
            f"SELECT player_id, status_name, expire_at FROM player_statuses "
            f"WHERE group_id = ? AND player_id IN ({placeholders}) AND expire_at > ? "
            f"ORDER BY player_id, expire_at"
        )
        async with self._db.execute(query, (group_id, *player_ids, now)) as cursor:
            rows = await cursor.fetchall()
        # 按 player_id 分组
        result_map = {}
        for pid, sname, exp in rows:
            remaining = self._calc_remaining_days(exp)
            result_map.setdefault(pid, []).append({
                "status_name": sname, "expire_at": exp, "remaining_days": remaining
            })
        return [(pid, result_map.get(pid, [])) for pid in player_ids]

    def _calc_remaining_days(self, expire_at: str) -> int:
        """把 expire_at（UTC 字符串）换算为剩余天数，不足一天算一天。

        两端必须同时区：expire_at 按 UTC 解析，当前时间也取 UTC。
        若这里用北京时间相减会因 naive 与 aware 混用直接抛 TypeError（批量查询曾因此崩掉）。
        """
        from datetime import datetime, timezone
        exp = datetime.strptime(expire_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        delta = exp - datetime.now(timezone.utc)
        return max(0, delta.days + (1 if delta.seconds > 0 else 0))

    async def get_player_statuses(self, group_id: str, player_id: str) -> list:
        """获取玩家未过期的状态列表。返回 [{"status_name": str, "expire_at": str, "remaining_days": int}, ...]"""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        async with self._db.execute(
            "SELECT status_name, expire_at FROM player_statuses "
            "WHERE group_id = ? AND player_id = ? AND expire_at > ? "
            "ORDER BY expire_at",
            (group_id, player_id, now)
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            {
                "status_name": r[0],
                "expire_at": r[1],
                "remaining_days": self._calc_remaining_days(r[1]),
            }
            for r in rows
        ]

    async def purge_expired_statuses(self) -> int:
        """清理所有过期状态记录。返回删除数量。

        由调度器的清理循环直接调用（没有外层事务），因此这里自行 commit——
        否则删除只存在于当前未提交事务中，连接关闭即丢失，还会被任何一次
        rollback 连带撤销。
        """
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        cursor = await self._db.execute(
            "DELETE FROM player_statuses WHERE expire_at <= ?",
            (now,)
        )
        await self._db.commit()
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

    async def rollback(self):
        """Rollback the current transaction. Used on error to discard uncommitted writes."""
        if self._db:
            try:
                await self._db.execute("ROLLBACK")
            except Exception:
                pass

    # === 待处理赠送 ===

    async def save_pending_gift(self, group_id: str, receiver_id: str,
                                 sender_id: str, sender_name: str,
                                 receiver_name: str, items_json: str) -> bool:
        """保存待处理赠送记录。若该接收方已有待处理赠送则返回 False（不覆盖已有记录）。"""
        try:
            await self._db.execute(
                "INSERT INTO pending_gifts "
                "(group_id, receiver_id, sender_id, sender_name, receiver_name, items_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (group_id, receiver_id, sender_id, sender_name, receiver_name, items_json)
            )
            await self._db.commit()
            return True
        except aiosqlite.IntegrityError:
            await self.rollback()
            return False

    async def get_pending_gift(self, group_id: str, receiver_id: str) -> Optional[dict]:
        """获取待处理赠送记录。返回 dict（含 created_at）或 None。"""
        async with self._db.execute(
            "SELECT sender_id, sender_name, receiver_name, items_json, created_at FROM pending_gifts "
            "WHERE group_id = ? AND receiver_id = ?",
            (group_id, receiver_id)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            import json
            return {
                "sender_id": row[0],
                "sender_name": row[1],
                "receiver_name": row[2],
                "items": json.loads(row[3]),
                "created_at": row[4],
            }

    async def delete_pending_gift(self, group_id: str, receiver_id: str) -> bool:
        """删除待处理赠送记录。返回是否真的删到了（并发下以此"认领"，只有一方能成功）。"""
        cursor = await self._db.execute(
            "DELETE FROM pending_gifts WHERE group_id = ? AND receiver_id = ?",
            (group_id, receiver_id)
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def get_expired_pending_gifts(self, max_age_seconds: int = 240) -> list:
        """获取所有超过 max_age_seconds 秒的待处理赠送记录。"""
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)).strftime("%Y-%m-%d %H:%M:%S")
        async with self._db.execute(
            "SELECT group_id, receiver_id, sender_id, sender_name, receiver_name, items_json "
            "FROM pending_gifts WHERE created_at <= ?",
            (cutoff,)
        ) as cursor:
            rows = await cursor.fetchall()
            result = []
            import json
            for row in rows:
                result.append({
                    "group_id": row[0],
                    "receiver_id": row[1],
                    "sender_id": row[2],
                    "sender_name": row[3],
                    "receiver_name": row[4],
                    "items": json.loads(row[5]),
                })
            return result

    # ── Prayer Daily Hits ──

    async def has_prayer_hit_today(self, group_id: str, player_id: str) -> bool:
        """检查玩家今日是否已触发过祷词。"""
        today = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
        async with self._db.execute(
            "SELECT 1 FROM prayer_daily_hits WHERE group_id=? AND player_id=? AND hit_date=?",
            (group_id, player_id, today)
        ) as cursor:
            return await cursor.fetchone() is not None

    async def record_prayer_hit(self, group_id: str, player_id: str, delta: int) -> bool:
        """记录祷词触发。唯一约束防并发重复。返回 True 表示成功记录。"""
        today = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
        try:
            await self._db.execute(
                "INSERT INTO prayer_daily_hits (group_id, player_id, hit_date, delta) VALUES (?, ?, ?, ?)",
                (group_id, player_id, today, delta)
            )
            await self._db.commit()
            return True
        except Exception:
            return False  # 并发重复触发

    # ── Gift Daily Accepts ──

    async def has_gift_accept_today(self, group_id: str, receiver_id: str) -> bool:
        """检查玩家今日是否已接受过道具。"""
        today = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
        async with self._db.execute(
            "SELECT 1 FROM gift_daily_accepts WHERE group_id=? AND receiver_id=? AND accept_date=?",
            (group_id, receiver_id, today)
        ) as cursor:
            return await cursor.fetchone() is not None

    async def count_gift_accepts_today(self, group_id: str, receiver_id: str) -> int:
        """统计玩家今日已接受道具次数。"""
        today = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
        async with self._db.execute(
            "SELECT COUNT(*) FROM gift_daily_accepts WHERE group_id=? AND receiver_id=? AND accept_date=?",
            (group_id, receiver_id, today)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def record_gift_accept(self, group_id: str, receiver_id: str) -> bool:
        """记录道具接受。返回 True 表示成功记录。"""
        today = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
        try:
            await self._db.execute(
                "INSERT INTO gift_daily_accepts (group_id, receiver_id, accept_date) VALUES (?, ?, ?)",
                (group_id, receiver_id, today)
            )
            await self._db.commit()
            return True
        except Exception:
            return False
