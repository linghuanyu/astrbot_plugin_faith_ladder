"""
数据库访问层。用 aiosqlite 单连接持有 ladder.db，负责建表、迁移与全部 SQL 操作。

等级字段的编码说明见 item_utils.grade_to_storage：解析侧是三态（None/''/等级），
存储侧是 NOT NULL 的文本（''/'-'/等级），因为 grade 参与 player_items 主键。
"""

import asyncio
import contextlib
import functools
import inspect
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

# 一次性数据迁移的记账名（存在 schema_migrations 表里）。
#
# **改名规则**：迁移逻辑本身一变（新增一类脏数据、换了判定条件），就必须换一个新名字。
# 否则老库上已有标记会让新逻辑永远不执行——这是"记账式迁移"唯一的坑。
MIGRATION_ITEM_NAMES = "item_names_star_suffix"
MIGRATION_ITEM_GRADE = "player_items_grade_split"

# player_statuses.source 的取值：祈愿组队写进去的状态。
# NULL 表示诸神手工添加——两者在表里同名同形，只能靠这一列分辨。
STATUS_SOURCE_WISH = "wish"

# wish_teams.status 的取值。departed 在 expire_at 之前仍是「活的」：
# 名单可增删，成员状态随之同步（见 db_manager 的祈愿试炼一节）。
WISH_RECRUITING = "recruiting"   # 招募中
WISH_DEPARTED = "departed"       # 已发车
WISH_DISBANDED = "disbanded"     # 已解散
WISH_VOIDED = "voided"           # 名额已满，未能成行

# 队名自动加序号的尝试上限。实际用不到这么多：同一名额日期的队伍数同时受
# 「群内招募中上限」与「该日期名额」限制，加序号只是为了绕开诸神改过的名字。
WISH_NAME_ATTEMPTS = 20


def _utc_now_stamp() -> str:
    """当前 UTC 时间戳字符串（与 expire_at / CURRENT_TIMESTAMP 同一格式）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _utc_stamp_after(days: float) -> str:
    """days 天之后的 UTC 时间戳字符串。"""
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def _utc_stamp_after_seconds(seconds: float) -> str:
    """seconds 秒之后的 UTC 时间戳字符串。

    秒级窗口（如储物空间彩蛋）用它算 expire_at。判定仍是字符串比较，
    格式与 `_utc_now_stamp()` 一致，所以字典序就是时间序；注意读侧的判定是
    **严格大于**，因此 seconds <= 0 写出来的记录一落库就已过期。
    """
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")

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

    # current_task() 在非任务上下文里是 None：拿它当持有者占位，保证重入判定
    # 依然成立（否则那种场景下的嵌套调用会自我阻塞）
    _NO_TASK = object()

    def __init__(self, data_dir: Path):
        """只记录数据目录与库文件路径；建表、迁移与连接建立都在 initialize() 里做。"""
        self.data_dir = data_dir
        self.db_path = data_dir / "ladder.db"
        self._db: Optional[aiosqlite.Connection] = None
        self._initialized = False
        # 串行化闸门：见 _write_guard / transaction
        self._lock = asyncio.Lock()
        self._lock_owner: Optional[asyncio.Task] = None

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
                block_actions TEXT DEFAULT NULL,
                source TEXT DEFAULT NULL,
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

            CREATE TABLE IF NOT EXISTS god_wagers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                god TEXT NOT NULL,
                action TEXT NOT NULL,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ends_at TIMESTAMP NOT NULL,
                settled INTEGER DEFAULT 0,
                winner_id TEXT,
                winner_name TEXT,
                participant_count INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS god_wager_entries (
                wager_id INTEGER NOT NULL,
                group_id TEXT NOT NULL,
                player_id TEXT NOT NULL,
                player_name TEXT NOT NULL,
                entered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (wager_id, player_id)
            );

            CREATE TABLE IF NOT EXISTS gift_daily_accepts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                receiver_id TEXT NOT NULL,
                accept_date TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_gift_accepts_lookup
                ON gift_daily_accepts(group_id, receiver_id, accept_date);

            -- 储物空间彩蛋的「窗口」：命中一次后的一段时间内，该玩家的每次查询
            -- 都重放同一条文案（读侧逻辑见 commands/query.py）。
            -- (group_id, player_id) 主键 = 每人最多一条窗口，重复触发即覆盖，
            -- 所以行数上界就是玩家数：不建 expire_at 索引、也不接每日清理，
            -- 过期只靠读时的 `expire_at > now` 判定（与 player_statuses 同口径）。
            CREATE TABLE IF NOT EXISTS inventory_easter_eggs (
                group_id TEXT NOT NULL,
                player_id TEXT NOT NULL,
                message TEXT NOT NULL,
                expire_at TIMESTAMP NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (group_id, player_id)
            );

            CREATE TABLE IF NOT EXISTS schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- 祈愿试炼（组队）。详见本文件末尾「祈愿试炼」一节。
            --
            -- slot_date  = 开团日 + 状态天数（北京日期），是「名额」与「每人每 slot
            --              只能参与一次」的归属键；
            -- create_date = 开团日（北京日期），只管两项每日开团次数上限。
            -- 两者只差几天却都叫「日期」，读写时务必不要混用。
            -- expire_at  = 发车时定下的到期时间，是**全队成员状态的唯一事实来源**
            --              （不是各自 now+days，否则补位进来的人会比队友晚到期）。
            CREATE TABLE IF NOT EXISTS wish_teams (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                name TEXT NOT NULL,
                capacity INTEGER NOT NULL CHECK (capacity > 1),
                leader_id TEXT NOT NULL,
                leader_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'recruiting',
                slot_date TEXT NOT NULL,
                create_date TEXT NOT NULL,
                expire_at TIMESTAMP DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                departed_at TIMESTAMP DEFAULT NULL,
                last_reminded_at TIMESTAMP DEFAULT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_wish_teams_group_status
                ON wish_teams(group_id, status);
            CREATE INDEX IF NOT EXISTS idx_wish_teams_slot
                ON wish_teams(group_id, slot_date, status);
            -- 队名群内**全局唯一**（不是只在招募中唯一）：队名同时是状态名和
            -- `祈愿加入` / `祈愿管理` 的定位键，而已发车的队伍在 3 天窗口内仍可被
            -- 加入与管理。若只约束招募中，就会出现「一个名字对应两支可操作的队」，
            -- 定位直接歧义。
            CREATE UNIQUE INDEX IF NOT EXISTS idx_wish_teams_name
                ON wish_teams(group_id, name);

            CREATE TABLE IF NOT EXISTS wish_team_members (
                team_id INTEGER NOT NULL,
                group_id TEXT NOT NULL,
                player_id TEXT NOT NULL,
                player_name TEXT NOT NULL,
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (team_id, player_id)
            );
            CREATE INDEX IF NOT EXISTS idx_wish_members_player
                ON wish_team_members(group_id, player_id);

            -- 每人每天开团次数（主键去重：插入成功即占到一个名额）
            CREATE TABLE IF NOT EXISTS wish_daily_creates (
                group_id TEXT NOT NULL,
                player_id TEXT NOT NULL,
                create_date TEXT NOT NULL,
                PRIMARY KEY (group_id, player_id, create_date)
            );

            -- 本群每天开团次数（靠 UPDATE ... WHERE count < ? 的 rowcount 认领）
            CREATE TABLE IF NOT EXISTS wish_daily_group_creates (
                group_id TEXT NOT NULL,
                create_date TEXT NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (group_id, create_date)
            );
        """)
        await self._db.commit()

        # Migrate old whitelist table (had group_id column) to global whitelist
        await self._migrate_whitelist()

        # Migrate: clean up old item names with *N suffix
        # （会全表扫 player_items 且逐行查一次，故走一次性记账）
        if not await self._migration_done(MIGRATION_ITEM_NAMES):
            await self._migrate_item_names()

        # Migrate: add grade column to player_items
        # （同样全表扫 + 逐行查；记账在方法内部，成功提交后才落标记）
        await self.migrate_player_items()

        # Migrate: rebuild player_items so the PK includes grade
        #
        # 这条**不进记账表**：它只做 PRAGMA table_info 判断，健康库上是 O(1)，
        # 没有性能收益；而它的判断条件会随代码演进变化，用标记跳过反而危险
        # （改了期望的主键却因为"迁移已做过"而不重建）。
        await self._migrate_items_grade_pk()

        # Migrate: add qq_id column to players (QQ binding for anti-impersonation)
        await self._migrate_qq_id()

        # Migrate: remove deprecated group entries from whitelist
        await self._migrate_whitelist_remove_groups()

        # 加列式迁移：全部幂等（列已在即跳过），列定义都来自代码常量。
        # 顺序有讲究：whitelist 那条必须排在 _migrate_whitelist 之后——
        # 那条迁移可能重建 whitelist 表，而重建出的新表没有 faith 列。
        for _table, _column, _ddl in (
            ("players", "oathbreaker", "INTEGER DEFAULT 0"),
            ("players", "specific_faith", "TEXT DEFAULT NULL"),
            ("whitelist", "faith", "TEXT DEFAULT NULL"),
            ("player_statuses", "block_actions", "TEXT DEFAULT NULL"),
            # source 记状态来源（'wish' = 祈愿组队写入，NULL = 诸神添加）。队伍状态与
            # 诸神手工状态在表里同名同形，靠名字或前缀识别都不可靠（诸神能改队名、
            # 也能手写同名状态），而「最多保留 N 条队伍状态」「名单变动时撤销对应
            # 状态」都要准确认出哪一行是队伍写的。
            ("player_statuses", "source", "TEXT DEFAULT NULL"),
        ):
            await self._ensure_column(_table, _column, _ddl)

    # === 一次性数据迁移记账 ===

    async def _migration_done(self, name: str) -> bool:
        """该一次性迁移是否已在成功提交后记过账。"""
        async with self._db.execute(
            "SELECT 1 FROM schema_migrations WHERE name = ?", (name,)
        ) as cursor:
            return await cursor.fetchone() is not None

    async def _mark_migration_done(self, name: str) -> None:
        """记账，表示该迁移已完成。**只在整趟成功且数据已提交之后调用。**

        顺序是「先提交数据、再记账」：若崩在两者之间，下次启动会重跑一遍，
        而这些迁移都幂等，重跑无副作用。反过来（先记账后提交）会留下
        "以为做过、其实没做完"的库，脏数据永远清不掉。
        """
        await self._db.execute(
            "INSERT OR REPLACE INTO schema_migrations (name, applied_at) "
            "VALUES (?, CURRENT_TIMESTAMP)",
            (name,),
        )
        await self._db.commit()
        logger.info(f"[Migration] {name} 已完成并记账，之后的启动不再重复扫描")

    # === 加列（幂等，不记账） ===

    async def _ensure_column(self, table: str, column: str, ddl: str) -> bool:
        """确保 table 有 column 列（ddl 形如 "TEXT DEFAULT NULL"），返回本次是否新增。

        「加列」此前在 5 个 `_migrate_*` 方法里各写了一遍 PRAGMA → ALTER → commit
        → log，增删一列就要跟着抄一遍；这里收敛成唯一实现，调用点是 initialize()
        里那张声明式列表。table / column / ddl 全部来自代码常量，不含用户输入。

        失败时**不静默**：这几列都是读查询依赖的列（`_PLAYER_COLUMNS`、状态卡片、
        白名单信仰判定），缺了会变成"插件能启动、但每条命令都抛 sqlite 错"，
        比启动失败更难定位（qq_id 唯一索引那次事故的同类教训）。所以这里把原始
        错误包成可诊断文案后原样抛出，让 operator 一眼看到是哪张表哪一列。
        """
        async with self._db.execute(f"PRAGMA table_info({table})") as cursor:
            columns = [row[1] for row in await cursor.fetchall()]
        if column in columns:
            return False

        try:
            await self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            await self._db.commit()
        except Exception as e:
            await self.rollback()
            raise RuntimeError(
                f"无法给表 {table} 加列 {column}：{e}。"
                "请检查数据目录里的 ladder.db 是否被其它进程占用或已损坏，"
                "以及插件文件是否版本混杂（升级需整目录覆盖，见 README「升级与部署」）。"
            ) from e
        logger.info(f"[Migration] Added {column} column to {table}")
        return True

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
            "SELECT rowid, group_id, player_id, item_name, grade, quantity FROM player_items"
        ) as cursor:
            rows = await cursor.fetchall()

        for rowid, group_id, player_id, item_name, grade, quantity in rows:
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
            # 目标行必须按 (名字, 等级) 定位：同名不同等级现在是各自独立的行，
            # 若只按名字找，会把 A 级那一行的数量改成"自己 + 脏数据"，
            # 无等级行的数量反而没变（此前正是如此，跨等级数量被污染）。
            async with self._db.execute(
                "SELECT rowid, quantity FROM player_items "
                "WHERE group_id = ? AND player_id = ? AND item_name = ? AND grade = ?",
                (group_id, player_id, clean_name, grade)
            ) as existing_cursor:
                existing = await existing_cursor.fetchone()

            if existing:
                # 合并进同等级的目标行，并删掉脏数据行（都按 rowid 定位，避免误伤）
                existing_rowid, existing_qty = existing
                await self._db.execute(
                    "UPDATE player_items SET quantity = ?, updated_at = CURRENT_TIMESTAMP WHERE rowid = ?",
                    (existing_qty + new_qty, existing_rowid)
                )
                await self._db.execute("DELETE FROM player_items WHERE rowid = ?", (rowid,))
            else:
                # 就地改名并折算数量
                await self._db.execute(
                    "UPDATE player_items SET item_name = ?, quantity = ? WHERE rowid = ?",
                    (clean_name, new_qty, rowid)
                )

        await self._db.commit()
        # 成功提交后才记账：此前每次启动都全表扫一遍 player_items 并逐行查重
        await self._mark_migration_done(MIGRATION_ITEM_NAMES)

    async def migrate_player_items(self, force: bool = False) -> int:
        """把旧数据里写在道具名中的等级拆到 grade 列，并清理重复行。

        '共生噬刃（C级）' → item_name='共生噬刃', grade='C'；目标行已存在时合并数量。
        幂等：启动时自动执行，也可用「天梯榜管理 迁移储物空间」手动重跑（force=True）。

        目标行的匹配必须带上 grade（存储形态），否则带等级的道具会被合并进
        同名无等级那一行、等级直接丢失（这正是本次修复的问题之一）。
        返回处理过的行数。

        已记过账时直接返回 0，不再全表扫描（这是启动期最重的一步：全表读 +
        逐行一次查询）。force=True 用于手动重跑。
        """
        if not force and await self._migration_done(MIGRATION_ITEM_GRADE):
            return 0
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
            # 没有脏数据可迁也要记账——否则"健康库"每次启动都得再全表扫一遍
            await self._mark_migration_done(MIGRATION_ITEM_GRADE)
            return migrated + merged
        except Exception as e:
            await self._db.rollback()
            # 不记账：下次启动（或「迁移储物空间」）会重试
            logger.error(f"[Migration] 道具等级迁移失败（下次启动会重试）: {e}")
            return 0

    async def _migrate_items_grade_pk(self):
        """重建 player_items，使主键包含 grade（(group, player, name, grade)）。

        旧主键下一个玩家无法同时持有「铁剑」和「铁剑（A级）」：后者会被合并进前者
        并丢掉等级，之后按等级赠送/扣除都会失败。SQLite 不能直接改主键，只能重建表。

        grade 同时归一化为 NOT NULL：NULL 在 UNIQUE/主键约束下互不相等，
        若允许 NULL，"无等级"道具会被反复插入成多行而不是合并。
        （无等级 → ''，有括号但非标准等级 → '-'，见 item_utils.grade_to_storage）

        **不用 executescript**：它会先隐式提交、并且不把脚本包在事务里。一旦在
        DROP 与 RENAME 之间失败，player_items 就消失了、数据留在 player_items_new，
        而下次启动的迁移又因为读不到源表而失败（异常被吞），插件会带着一张不存在的
        表继续运行、用户道具全部不可见。

        改为逐一 execute。注意 Python sqlite3 在 legacy 模式下**只为 DML 开启隐式
        事务**（DDL 不开启），所以精确的行为是：CREATE 自动提交 → INSERT 开启事务
        → 其后的 DROP/ALTER 受该事务保护。也就是说真正致命的 DROP 是可回滚的
        （失败时 player_items 完好），只会残留一张空的 player_items_new，
        由 _recover_interrupted_grade_pk_migration() 在下次启动时清理。
        """
        # 必须先处理上次中断留下的暂存表，再判断主键：
        # 中断后 _create_tables 会用新结构重建一张空的 player_items（已含 grade 主键），
        # 那样下面的主键判断会提前返回，暂存表里的旧数据就永远找不回来了。
        await self._recover_interrupted_grade_pk_migration()

        async with self._db.execute("PRAGMA table_info(player_items)") as cursor:
            info = await cursor.fetchall()
        pk_cols = [r[1] for r in info if r[5]]
        if "grade" in pk_cols:
            return
        if not pk_cols:
            # 表不存在（异常状态）：不冒险重建，留待下次启动或人工处理
            logger.error("[Migration] player_items 表缺失，跳过主键重建")
            return

        logger.info(f"[Migration] 重建 player_items 主键：{pk_cols} → 加入 grade")
        try:
            await self._db.execute(
                "CREATE TABLE IF NOT EXISTS player_items_new ("
                "  group_id TEXT NOT NULL, player_id TEXT NOT NULL, item_name TEXT NOT NULL,"
                "  grade TEXT NOT NULL DEFAULT '', quantity INTEGER DEFAULT 1,"
                "  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
                "  PRIMARY KEY (group_id, player_id, item_name, grade))"
            )
            await self._db.execute(
                "INSERT INTO player_items_new (group_id, player_id, item_name, grade, quantity, updated_at) "
                "SELECT group_id, player_id, item_name, "
                "       CASE WHEN grade IS NULL THEN '' ELSE grade END, quantity, updated_at "
                "FROM player_items"
            )
            await self._db.execute("DROP TABLE player_items")
            await self._db.execute("ALTER TABLE player_items_new RENAME TO player_items")
            await self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_player_items_lookup ON player_items(group_id, player_id)"
            )
            await self._db.commit()
            logger.info("[Migration] player_items 重建完成")
        except Exception as e:
            await self._db.rollback()
            logger.error(f"[Migration] player_items 主键重建失败，已回滚（下次启动会重试）: {e}")

    async def _table_exists(self, name: str) -> bool:
        """表是否存在（仅用于内部迁移判断，name 来自代码常量）。"""
        async with self._db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ) as cursor:
            return await cursor.fetchone() is not None

    async def _recover_interrupted_grade_pk_migration(self) -> None:
        """修复「主键重建中途失败」留下的状态。

        重建顺序是 CREATE player_items_new → INSERT → DROP player_items → RENAME。
        中断后必然留下 player_items_new，分两种情形：
          A. player_items 已不存在（DROP 成功、RENAME 未执行）
          B. player_items 已被 _create_tables 按新结构重建为空表，旧数据仍在暂存表里

        两者都应以暂存表的数据为准恢复，否则用户道具会凭空消失（B 尤其隐蔽，
        因为空表已经带 grade 主键，主键判断会认为"已迁移完成"）。
        若主表已有数据，则暂存表视为陈留垃圾直接丢弃。
        """
        if not await self._table_exists("player_items_new"):
            return

        if await self._table_exists("player_items"):
            async with self._db.execute("SELECT COUNT(*) FROM player_items_new") as cursor:
                new_rows = (await cursor.fetchone())[0]
            async with self._db.execute("SELECT COUNT(*) FROM player_items") as cursor:
                main_rows = (await cursor.fetchone())[0]
            if new_rows == 0 or main_rows > 0:
                logger.warning(
                    f"[Migration] 丢弃中断遗留的暂存表 player_items_new"
                    f"（暂存 {new_rows} 行 / 主表 {main_rows} 行）"
                )
                await self._db.execute("DROP TABLE player_items_new")
                await self._db.commit()
                return
            logger.warning(
                f"[Migration] 上次重建中断：player_items 为空、暂存表有 {new_rows} 行，改用暂存表恢复"
            )
            await self._db.execute("DROP TABLE player_items")
        else:
            logger.warning("[Migration] 上次重建中断：player_items 缺失，从暂存表恢复")

        await self._db.execute("ALTER TABLE player_items_new RENAME TO player_items")
        await self._db.commit()

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
        # （迁移中途失败、或由更早的版本建库），那种库会永久失去 QQ 唯一性约束。
        #
        # 但老库可能已经积累了重复 qq_id（旧版本没有捕获唯一冲突），
        # 此时 CREATE UNIQUE INDEX 会抛 IntegrityError 并逃出 initialize()，
        # 让插件每次启动都失败、且没有任何补救路径。故这里 fail-soft：
        # 记录重复行、跳过建索引、明确告警，插件先能跑起来。
        try:
            await self._db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_players_qq "
                "ON players(group_id, qq_id)"
            )
            await self._db.commit()
        except aiosqlite.IntegrityError:
            await self.rollback()
            async with self._db.execute(
                "SELECT group_id, qq_id, COUNT(*) FROM players "
                "WHERE qq_id IS NOT NULL GROUP BY group_id, qq_id HAVING COUNT(*) > 1"
            ) as cursor:
                dups = await cursor.fetchall()
            logger.error(
                "[Migration] 无法创建 QQ 唯一索引：players 表存在重复绑定 "
                f"{dups}。本次跳过建索引，QQ 唯一性暂不生效；"
                "请用「换绑QQ」把重复的 QQ 改到不同玩家后再重启插件。"
            )

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
        """获取白名单列表，包含信仰字段。仅返回已授权的 user 条目。

        过滤放在 SQL 里：调用方此前各自再过滤一遍 entry_type，
        漏掉一处就会把非授权条目（如待审）当成诸神展示出去。
        """
        async with self._db.execute(
            "SELECT entry_type, entry_id, faith, added_by, added_at FROM whitelist "
            "WHERE entry_type = 'user' ORDER BY id"
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

    # --- 待审白名单：入群先进待审，超管确认后才授权 ---
    #
    # 复用 whitelist 表的 entry_type 列承载 'pending'：所有授权路径都写死
    # entry_type = 'user'，所以待审条目天然不授权，也不需要新表或迁移。

    async def _whitelist_row_exists(self, entry_type: str, entry_id: str) -> bool:
        """该 (类型, id) 是否已有行。私有：只在已持有写闸门的方法内部调用。"""
        async with self._db.execute(
            "SELECT 1 FROM whitelist WHERE entry_type = ? AND entry_id = ?",
            (entry_type, entry_id)
        ) as cursor:
            return await cursor.fetchone() is not None

    async def _whitelist_row_exists_any(self, entry_id: str) -> bool:
        """该 id 在白名单里是否有任何类型的条目（user 或 pending）。"""
        async with self._db.execute(
            "SELECT 1 FROM whitelist WHERE entry_id = ?",
            (entry_id,)
        ) as cursor:
            return await cursor.fetchone() is not None

    async def get_pending_whitelist(self) -> List[dict]:
        """待审条目，按加入顺序返回。"""
        async with self._db.execute(
            "SELECT entry_id, added_by, added_at FROM whitelist "
            "WHERE entry_type = 'pending' ORDER BY id"
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                {"entry_id": r[0], "added_by": r[1], "added_at": r[2]}
                for r in rows
            ]

    async def _approve_pending_locked(self, entry_id: str) -> bool:
        """待审转正的落库动作。调用方负责事务与提交。"""
        if await self._whitelist_row_exists("user", entry_id):
            # 已经是诸神：待审行只是冗余（UNIQUE 只约束 entry_type+entry_id，两者可并存）
            cursor = await self._db.execute(
                "DELETE FROM whitelist WHERE entry_type = 'pending' AND entry_id = ?",
                (entry_id,)
            )
        else:
            cursor = await self._db.execute(
                "UPDATE whitelist SET entry_type = 'user' "
                "WHERE entry_type = 'pending' AND entry_id = ?",
                (entry_id,)
            )
        return cursor.rowcount > 0

    async def approve_pending(self, entry_id: str) -> bool:
        """把一条待审条目转成正式诸神，返回是否确实发生了转换。"""
        async with self.transaction():
            return await self._approve_pending_locked(entry_id)

    async def approve_all_pending(self) -> int:
        """把所有待审条目转为诸神，返回转换条数（同一事务内完成）。"""
        async with self.transaction():
            async with self._db.execute(
                "SELECT entry_id FROM whitelist WHERE entry_type = 'pending' ORDER BY id"
            ) as cursor:
                ids = [r[0] for r in await cursor.fetchall()]
            converted = 0
            for entry_id in ids:
                if await self._approve_pending_locked(entry_id):
                    converted += 1
            return converted

    async def reject_pending(self, entry_id: str) -> bool:
        """丢弃一条待审条目（不授权）。返回是否删掉了一行。"""
        cursor = await self._db.execute(
            "DELETE FROM whitelist WHERE entry_type = 'pending' AND entry_id = ?",
            (entry_id,)
        )
        await self._db.commit()
        return cursor.rowcount > 0

    async def remove_whitelist_entry_everywhere(self, entry_id: str) -> int:
        """删除该 id 的全部白名单条目（user 与 pending），返回删除行数。

        退群时用：只删 user 会把待审行留在超管的待审列表里阴魂不散。
        """
        cursor = await self._db.execute(
            "DELETE FROM whitelist WHERE entry_id = ? AND entry_type IN ('user', 'pending')",
            (entry_id,)
        )
        await self._db.commit()
        return cursor.rowcount

    async def add_pending(self, entry_id: str, added_by: str) -> bool:
        """记入待审名单（不授权）。返回是否真的新增了一条。

        守卫是"该 id 在白名单里已有任何条目就跳过"：唯一键只是
        (entry_type, entry_id)，光靠 INSERT OR IGNORE 挡不住"已是诸神的人
        入群又拿到一条 pending"，那种冗余行会让他出现在待审列表里等着被批准。
        """
        entry_id = str(entry_id).strip()
        if not entry_id:
            return False
        async with self.transaction():
            if await self._whitelist_row_exists_any(entry_id):
                return False
            cursor = await self._db.execute(
                "INSERT INTO whitelist (entry_type, entry_id, added_by) "
                "VALUES ('pending', ?, ?)",
                (entry_id, added_by)
            )
            return cursor.rowcount > 0

    async def add_many_pending(self, entry_ids, added_by: str) -> int:
        """批量记入待审名单，返回实际新增条数。

        先读一次现有 id、再只插差集：写锁在手，读-写之间没有并发窗口，
        计数因此是精确的（不依赖 INSERT...SELECT 的 rowcount 语义）。
        差集按 id 排序后写入，让待审列表与回复文案的顺序稳定可复现
        （调用方传进来的常常是 set，迭代顺序随 hash 随机化）。
        """
        ids = list(dict.fromkeys(str(i).strip() for i in entry_ids if str(i).strip()))
        if not ids:
            return 0
        async with self.transaction():
            async with self._db.execute("SELECT entry_id FROM whitelist") as cursor:
                existing = {str(r[0]) for r in await cursor.fetchall()}
            fresh = sorted(i for i in ids if i not in existing)
            for entry_id in fresh:
                await self._db.execute(
                    "INSERT INTO whitelist (entry_type, entry_id, added_by) "
                    "VALUES ('pending', ?, ?)",
                    (entry_id, added_by)
                )
            return len(fresh)

    async def remove_many_from_whitelist(self, entry_ids, entry_type: str = "user") -> int:
        """批量移除指定类型的条目，返回删除行数。"""
        ids = list(dict.fromkeys(str(i).strip() for i in entry_ids if str(i).strip()))
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        cursor = await self._db.execute(
            f"DELETE FROM whitelist WHERE entry_type = ? AND entry_id IN ({placeholders})",
            (entry_type, *ids)
        )
        await self._db.commit()
        return cursor.rowcount

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
        # 按 rowcount 判定：玩家不存在时 UPDATE 匹配 0 行，此前会照样回"换绑成功"
        try:
            cursor = await self._db.execute(
                "UPDATE players SET qq_id = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE group_id = ? AND player_id = ?",
                (new_qq, group_id, player_id)
            )
        except aiosqlite.IntegrityError:
            # 并发下另一个请求刚把该 QQ 绑给了别人（上面第 2 步检查之后）
            await self.rollback()
            return False, f"QQ {new_qq} 已被其他玩家绑定，请先让其换绑或解绑。", old_qq
        if cursor.rowcount <= 0:
            await self.rollback()
            return False, "该玩家不在本宇宙，未做任何修改。", old_qq
        await self._db.commit()
        return True, "换绑成功", old_qq

    async def get_top_players(self, group_id: str, limit: int = 10, min_ladder_score: int = 0) -> List[Player]:
        """Get top players by ladder score for a group.

        min_ladder_score 是榜单门槛（低于该分不上榜）。过滤必须放在 SQL 里：
        先按 LIMIT 取人、再在上层丢弃低分玩家会让榜上人数不足（低分者占掉了名额）。
        """
        async with self._db.execute(
            "SELECT player_id, group_id, player_name, class, faith, specific_faith, ladder_score, pilgrimage_score, created_at, updated_at, oathbreaker, qq_id FROM players WHERE group_id = ? AND ladder_score >= ? ORDER BY ladder_score DESC LIMIT ?",
            (group_id, min_ladder_score, limit)
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
            return False, f"在本宇宙未寻找到（{old_name}）"

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
            await self.rollback()
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

    # --- Score history retention ---

    async def purge_old_score_history(self, retention_days: int = 90) -> int:
        """删除超过 retention_days 的积分历史，返回删除行数。

        由调度器的每日任务调用（因此自行 commit）。
        SQLite 的 CURRENT_TIMESTAMP 是 UTC，故截止时间也用 UTC 计算。
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).strftime("%Y-%m-%d %H:%M:%S")
        cursor = await self._db.execute(
            "DELETE FROM score_history WHERE timestamp < ?",
            (cutoff,)
        )
        await self._db.commit()
        return cursor.rowcount

    async def purge_daily_tables(self, retention_days: int = 90) -> int:
        """删除超过 retention_days 的每日状态记录（赠送接受次数、祷词触发、开团次数）。

        这些表按「北京日期」存 `YYYY-MM-DD` 字符串，可直接按字典序比较。
        此前从未清理过：每次接受道具一行、每人每天一行，随使用量无限增长。
        同样由调度器每日调用，故自行 commit。
        """
        cutoff = (datetime.now(BEIJING_TZ) - timedelta(days=retention_days)).strftime("%Y-%m-%d")
        cursor = await self._db.execute(
            "DELETE FROM gift_daily_accepts WHERE accept_date < ?", (cutoff,)
        )
        deleted = cursor.rowcount
        cursor = await self._db.execute(
            "DELETE FROM prayer_daily_hits WHERE hit_date < ?", (cutoff,)
        )
        deleted += cursor.rowcount
        cursor = await self._db.execute(
            "DELETE FROM wish_daily_creates WHERE create_date < ?", (cutoff,)
        )
        deleted += cursor.rowcount
        cursor = await self._db.execute(
            "DELETE FROM wish_daily_group_creates WHERE create_date < ?", (cutoff,)
        )
        deleted += cursor.rowcount
        await self._db.commit()
        return deleted

    # --- Backup ---

    async def backup_to(self, backup_path: Path) -> None:
        """用 SQLite 的在线备份 API 生成一致性快照到 backup_path。

        三点要点：
        - **另开一个独立连接**执行备份，绝不在共享连接上 commit。多步处理器会跨
          await 持有事务，若在这里 commit，会把它们写了一半的中间状态落盘，
          破坏 录入玩家/批量录入/收回道具 的原子性。
        - 用 `Connection.backup()` 而不是 `VACUUM INTO`：后者要求「不在事务中」，
          在共享连接上随时可能因为别人持有事务而失败；备份 API 对 BUSY/LOCKED
          自带重试，且只读源库、不改动主连接状态。
        - 备份是阻塞操作，放到线程里执行。
        失败时抛异常，由调用方决定重试（调度器的每日守卫会重试）。
        """
        if self._db is None:
            raise RuntimeError("数据库尚未初始化，无法备份")
        await asyncio.to_thread(self._backup_sync, Path(backup_path))

    def _backup_sync(self, dest: Path) -> None:
        """在独立连接上做备份（阻塞，供 asyncio.to_thread 调用）。"""
        import sqlite3

        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest.unlink()
        src = sqlite3.connect(str(self.db_path))
        try:
            dst = sqlite3.connect(str(dest))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()

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
        """减少道具。quantity=None 时删除该行。grade 为解析侧三态等级。

        匹配范围：grade 一律精确匹配一行——
        - 指定 grade → 该等级那一行
        - grade=None → "无等级"那一行（存储为 ''）

        None **不再**表示"不过滤等级"。同名不同等级现在是各自独立的行，若把 None
        当作"所有等级"，「收回道具 张三 铁剑」（全部收回）会把铁剑的无等级、A 级、
        C 级一起删掉，却只报无等级那一行的数量。需要清空某道具的所有等级时，
        请用 clear_items（对应指令「清除储物空间 <玩家> <道具名>」）。

        require_sufficient=False（默认，保持旧语义）：数量不足时截断到 0，只要命中行就返回 True。
        require_sufficient=True：数量不足时不改动数据并返回 False，供并发下"扣到才算成功"的场景。
        返回是否成功扣减（没有命中任何行时为 False）。
        """
        base_where = "group_id = ? AND player_id = ? AND item_name = ?"
        base_params = (group_id, player_id, item_name)
        grade_filter = GRADE_STORAGE_NONE if grade is None else grade_to_storage(grade)
        where = base_where + " AND grade = ?"
        params = base_params + (grade_filter,)

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

    # ── 储物空间彩蛋窗口 ──

    async def get_active_inventory_easter_egg(self, group_id: str, player_id: str) -> Optional[str]:
        """取该玩家当前未过期的彩蛋文案；没有窗口（或已过期）返回 None。

        只读，不删过期行：留着无害——行数上界就是玩家数，且下次触发会覆盖。
        判定是严格大于（`expire_at > now`），两侧同为 UTC 秒级字符串，字典序即时间序。
        """
        async with self._db.execute(
            "SELECT message FROM inventory_easter_eggs "
            "WHERE group_id = ? AND player_id = ? AND expire_at > ?",
            (group_id, player_id, _utc_now_stamp()),
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else None

    async def set_inventory_easter_egg(
        self, group_id: str, player_id: str, message: str, seconds: float
    ) -> None:
        """写下彩蛋窗口：seconds 秒内该玩家的每次查询都重放这条文案。

        自行 commit——调用方在命令路径上，没有外层事务（同 create_wager 的约定）。
        seconds <= 0 写出的记录一落库就已过期（读侧判定严格大于），调用方应避免。
        """
        await self._db.execute(
            "INSERT INTO inventory_easter_eggs (group_id, player_id, message, expire_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(group_id, player_id) DO UPDATE SET "
            "message = excluded.message, expire_at = excluded.expire_at",
            (group_id, player_id, message, _utc_stamp_after_seconds(seconds)),
        )
        await self._db.commit()

    # === 状态 ===

    async def add_status(
        self, group_id: str, player_id: str, status_name: str, days: int,
        block_actions: Optional[str] = None,
        source: Optional[str] = None,
    ) -> None:
        """添加状态。从当前时间开始持续 days 天。

        expire_at 以 UTC 存储（与 score_history / CURRENT_TIMESTAMP 一致），
        因此所有比较与到期判定都必须用 UTC；调用方负责 commit。

        block_actions：None = 保持原有阻断项不变（续期时不顺带清掉）；
        字符串 = 覆盖（空串表示清除阻断）。存的是逗号分隔的动作 id。

        source：只在**插入新行**时写入，命中已有行时不参与更新。来源是"这条状态
        归谁管"，续期不该改变它——诸神给一条队伍状态续期，不该把它从祈愿组队手里
        收走，反向同理。（祈愿组队自己挂状态用的是 set_wish_status_until。）
        """
        if days <= 0:
            return
        from datetime import datetime, timedelta, timezone
        expire_at = (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

        columns = ["group_id", "player_id", "status_name", "expire_at"]
        values = [group_id, player_id, status_name, expire_at]
        updates = ["expire_at = excluded.expire_at"]
        if block_actions is not None:
            columns.append("block_actions")
            values.append(block_actions)
            updates.append("block_actions = excluded.block_actions")
        if source is not None:
            columns.append("source")
            values.append(source)

        await self._db.execute(
            f"INSERT INTO player_statuses ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' * len(values))}) "
            f"ON CONFLICT(group_id, player_id, status_name) DO UPDATE SET {', '.join(updates)}",
            tuple(values),
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

    async def _select_status_expiry(
        self, group_id: str, player_id: str, status_name: str, source_filter: Optional[str] = None
    ) -> Optional[str]:
        """取某条状态的 expire_at；不存在返回 None。可按来源过滤。"""
        sql = (
            "SELECT expire_at FROM player_statuses "
            "WHERE group_id = ? AND player_id = ? AND status_name = ?"
        )
        params = [group_id, player_id, status_name]
        if source_filter is not None:
            sql += " AND source = ?"
            params.append(source_filter)
        async with self._db.execute(sql, tuple(params)) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else None

    async def _rename_status_core(
        self,
        group_id: str,
        player_id: str,
        old_name: str,
        new_name: str,
        source_filter: Optional[str] = None,
    ) -> str:
        """改状态名的内核。返回 `not_found` / `ok` / `merged`。不提交。

        为什么必须**先查后改**：status_name 是主键的一部分。SQLite 允许 UPDATE
        主键列，但结果一旦与另一行撞主键，整条语句直接失败——所以得先知道目标名
        是否存在。

        合并规则：目标行的 expire_at 抬到两者中更晚的一个，block_actions 与 source
        保留**目标行原有的**。到期取更晚是为了不让改名缩短惩罚；阻断项同理——它属于
        那条惩罚本身，不该因为改名而被清掉。

        source_filter：只改「旧名那一行」的来源（队伍改名时用它锁定 `wish` 状态，
        免得把诸神手写的同名状态搬走）。**目标名的查重不带过滤**——目标行是谁的
        都算撞名，必须走合并，否则原地改名会撞主键。
        """
        if not old_name or not new_name:
            return "not_found"

        old_expiry = await self._select_status_expiry(
            group_id, player_id, old_name, source_filter
        )
        if old_expiry is None:
            return "not_found"

        new_expiry = await self._select_status_expiry(group_id, player_id, new_name)
        if old_name == new_name or new_expiry is None:
            # 原地改名（old == new 时这条 UPDATE 是空操作，省一次判断分支）
            sql = (
                "UPDATE player_statuses SET status_name = ? "
                "WHERE group_id = ? AND player_id = ? AND status_name = ?"
            )
            params = [new_name, group_id, player_id, old_name]
            if source_filter is not None:
                sql += " AND source = ?"
                params.append(source_filter)
            await self._db.execute(sql, tuple(params))
            return "ok"

        await self._db.execute(
            "UPDATE player_statuses SET expire_at = ? "
            "WHERE group_id = ? AND player_id = ? AND status_name = ?",
            (max(old_expiry, new_expiry), group_id, player_id, new_name),
        )
        sql = "DELETE FROM player_statuses WHERE group_id = ? AND player_id = ? AND status_name = ?"
        params = [group_id, player_id, old_name]
        if source_filter is not None:
            sql += " AND source = ?"
            params.append(source_filter)
        await self._db.execute(sql, tuple(params))
        return "merged"

    async def rename_status(
        self, group_id: str, player_id: str, old_name: str, new_name: str
    ) -> str:
        """把一条状态改名，保留 expire_at / block_actions / source。返回结果码。

        结果码：`not_found`（旧名不存在）/ `ok`（原地改名）/ `merged`（目标名已存在，
        两条并为一条）。调用方负责 commit（与 add_status / remove_status 一致）。
        """
        return await self._rename_status_core(group_id, player_id, old_name, new_name)

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
            f"SELECT player_id, status_name, expire_at, block_actions, source FROM player_statuses "
            f"WHERE group_id = ? AND player_id IN ({placeholders}) AND expire_at > ? "
            f"ORDER BY player_id, expire_at"
        )
        async with self._db.execute(query, (group_id, *player_ids, now)) as cursor:
            rows = await cursor.fetchall()
        # 按 player_id 分组
        result_map = {}
        for pid, sname, exp, block_actions, source in rows:
            remaining = self._calc_remaining_days(exp)
            result_map.setdefault(pid, []).append({
                "status_name": sname, "expire_at": exp, "remaining_days": remaining,
                "block_actions": block_actions, "source": source,
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
        """获取玩家未过期的状态列表。

        返回 [{"status_name", "expire_at", "remaining_days", "block_actions", "source"}, ...]
        source 为 'wish' 表示这条状态由祈愿组队写入（见 STATUS_SOURCE_WISH）。
        """
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        async with self._db.execute(
            "SELECT status_name, expire_at, block_actions, source FROM player_statuses "
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
                "block_actions": r[2],
                "source": r[3],
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

    # ── 神明的赌局 ──

    async def create_wager(self, group_id: str, god: str, action: str, seconds: int) -> Optional[int]:
        """开一场赌局，返回赌局 id。自行 commit（调用方在调度循环里，没有外层事务）。"""
        from datetime import datetime, timedelta, timezone
        ends_at = (datetime.now(timezone.utc) + timedelta(seconds=max(0, int(seconds)))).strftime("%Y-%m-%d %H:%M:%S")
        cursor = await self._db.execute(
            "INSERT INTO god_wagers (group_id, god, action, ends_at) VALUES (?, ?, ?, ?)",
            (group_id, god, action, ends_at),
        )
        await self._db.commit()
        return cursor.lastrowid

    async def get_last_wager_started_at(self, group_id: str) -> Optional[float]:
        """该群最近一次赌局的开局时间（Unix 秒，UTC）；没有记录返回 None。

        用于跨重启的间隔判定：内存里的计时器会随插件重载清零，靠这张表兜底。
        """
        from datetime import datetime, timezone
        async with self._db.execute(
            "SELECT started_at FROM god_wagers WHERE group_id = ? ORDER BY id DESC LIMIT 1",
            (group_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if not row or not row[0]:
            return None
        try:
            text = str(row[0]).split(".")[0]
            dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
            return dt.replace(tzinfo=timezone.utc).timestamp()
        except Exception:
            return None

    async def add_wager_entry(self, wager_id: int, group_id: str, player_id: str, player_name: str) -> bool:
        """记录一次入局；同一场赌局内同一玩家只记一次。"""
        try:
            await self._db.execute(
                "INSERT INTO god_wager_entries (wager_id, group_id, player_id, player_name) "
                "VALUES (?, ?, ?, ?)",
                (wager_id, group_id, player_id, player_name),
            )
            await self._db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False  # 已入局（主键拦住）
        except Exception as e:
            await self.rollback()
            logger.error(f"[Wager] 记录入局失败: {e}")
            return False

    async def finish_wager(self, wager_id: int, winner_id: Optional[str], winner_name: Optional[str], count: int) -> None:
        """结算落库（谁赢了、多少人参与）。"""
        await self._db.execute(
            "UPDATE god_wagers SET settled = 1, winner_id = ?, winner_name = ?, participant_count = ? "
            "WHERE id = ?",
            (winner_id, winner_name, int(count), wager_id),
        )
        await self._db.commit()

    async def close(self):
        """Close the persistent database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    @contextlib.asynccontextmanager
    async def _write_guard(self):
        """串行化闸门；同一任务的嵌套调用直接放行（否则会自调用死锁）。

        aiosqlite 只串行化**单条**语句：一条逻辑操作的多个 await 之间，别的命令
        可以插进来执行并 commit()，把这里的半成品一起提交掉——"原子提交"就只是
        文档里的一句话。单连接 SQLite 本来就是串行资源，所以对外方法统一排队。
        """
        task = asyncio.current_task() or self._NO_TASK
        if task is self._lock_owner:
            yield
            return
        async with self._lock:
            self._lock_owner = task
            try:
                yield
            finally:
                self._lock_owner = None

    @contextlib.asynccontextmanager
    async def transaction(self):
        """多步写入的原子边界：正常退出统一提交，中途抛错则整体回滚。

        块内调用各写方法时传 `commit=False`，不要自己 commit。
        """
        async with self._write_guard():
            try:
                yield
            except BaseException:
                await self.rollback()
                raise
            await self.commit()

    async def commit(self):
        """Commit the current transaction. Exposed for multi-step atomic operations."""
        if self._db:
            await self._db.commit()

    async def rollback(self):
        """Rollback the current transaction. Used on error to discard uncommitted writes."""
        if self._db:
            try:
                await self._db.rollback()
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

    async def get_prayer_streak(self, group_id: str, player_id: str, max_days: int = 400) -> int:
        """连续祷词天数（含今天）。

        按北京日期字符串逐日回溯，不做时区换算——写入端用的是同一个字符串格式，
        直接比较既简单也不会因时区转换出偏差。当天还没记录时返回 0。
        """
        from datetime import timedelta

        async with self._db.execute(
            "SELECT hit_date FROM prayer_daily_hits WHERE group_id=? AND player_id=? "
            "ORDER BY hit_date DESC LIMIT ?",
            (group_id, player_id, max_days)
        ) as cursor:
            rows = await cursor.fetchall()

        dates = {r[0] for r in rows}
        if not dates:
            return 0

        streak = 0
        day = datetime.now(BEIJING_TZ).date()
        while day.strftime("%Y-%m-%d") in dates:
            streak += 1
            day -= timedelta(days=1)
        return streak

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
        except aiosqlite.IntegrityError:
            return False  # 并发重复触发（唯一约束拦住）
        except Exception as e:
            # 其它错误（磁盘/表结构等）不能当成"重复"静默吞掉
            await self.rollback()
            logger.error(f"[Prayer] 记录祷词触发失败: {e}")
            return False

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
        except Exception as e:
            # 该表没有唯一约束，正常插入不会冲突；出错即真实故障，必须留痕
            await self.rollback()
            logger.error(f"[Gift] 记录接受道具次数失败: {e}")
            return False


    # === 祈愿试炼（组队）===
    #
    # 模型：队伍发车后在 expire_at 之前是「活的」——名单可增删，成员状态随之同步。
    # 状态名 = 队名，到期时间 = **队伍的 expire_at**（不是各自 now + days），所以
    # 队伍的 expire_at 是全队状态的唯一事实来源：发车、补位、换人三条授予路径都必须
    # 写它，任何一处误用 now + days 都会让队友之间的到期时间漂开，「队名 = 同一场
    # 试炼」就不成立了。
    #
    # 每个对外方法都是一次完整操作：内部开一个 transaction()，把名单改动与状态改动
    # 一起提交，不允许留下「名单改了、状态没跟上」的中间态。

    _WISH_TEAM_COLUMNS = (
        "id, group_id, name, capacity, leader_id, leader_name, status, "
        "slot_date, create_date, expire_at, created_at, departed_at, last_reminded_at"
    )

    # ── 内部辅助（只在 transaction() 内调用，不自行提交）──

    @staticmethod
    def _row_to_wish_team(row) -> dict:
        return {
            "team_id": row[0], "group_id": row[1], "name": row[2], "capacity": row[3],
            "leader_id": row[4], "leader_name": row[5], "status": row[6],
            "slot_date": row[7], "create_date": row[8], "expire_at": row[9],
            "created_at": row[10], "departed_at": row[11], "last_reminded_at": row[12],
        }

    async def _wish_team_row(self, team_id: int) -> Optional[dict]:
        async with self._db.execute(
            f"SELECT {self._WISH_TEAM_COLUMNS} FROM wish_teams WHERE id = ?", (team_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return self._row_to_wish_team(row) if row else None

    async def _wish_members(self, team_id: int) -> List[dict]:
        """队员，按加入顺序。

        排序键必须带 rowid：`joined_at` 是 TIMESTAMP，只有秒级精度，而一支队伍
        通常在几秒内凑齐——同一秒加入的人若只按 player_id 兜底，顺序就退化成
        字符比较；「队长交接到最早加入的剩余成员」会因此挑错人。rowid 随插入
        递增，是这里唯一靠得住的顺序依据。
        """
        async with self._db.execute(
            "SELECT player_id, player_name FROM wish_team_members "
            "WHERE team_id = ? ORDER BY joined_at, rowid",
            (team_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [{"player_id": r[0], "player_name": r[1]} for r in rows]

    async def _wish_team_with_members(self, team_id: int) -> Optional[dict]:
        team = await self._wish_team_row(team_id)
        if team is None:
            return None
        team["members"] = await self._wish_members(team_id)
        return team

    async def _wish_is_member(self, team_id: int, player_id: str) -> bool:
        async with self._db.execute(
            "SELECT 1 FROM wish_team_members WHERE team_id = ? AND player_id = ?",
            (team_id, player_id),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def _wish_status_expiry(
        self, group_id: str, player_id: str, status_name: str
    ) -> Optional[str]:
        """该玩家名下这个状态名是否还有效（未过期）；有则返回 expire_at。

        不按来源过滤：诸神手写的同名状态同样会挡住加入（见「同名状态下不能加入」）。
        """
        async with self._db.execute(
            "SELECT expire_at FROM player_statuses "
            "WHERE group_id = ? AND player_id = ? AND status_name = ? AND expire_at > ?",
            (group_id, player_id, status_name, _utc_now_stamp()),
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else None

    @staticmethod
    def _wish_window_open(team: dict) -> bool:
        """已发车队伍的 3 天窗口是否还开着（窗口内名单可增删）。"""
        expire_at = team.get("expire_at")
        return bool(expire_at) and expire_at > _utc_now_stamp()

    async def _wish_active_team_for_player(
        self, group_id: str, player_id: str, exclude_team_id: Optional[int] = None
    ) -> Optional[dict]:
        """该玩家在本群进行中的队伍：招募中，或已发车但窗口未关。"""
        sql = (
            "SELECT t.id FROM wish_teams t JOIN wish_team_members m ON m.team_id = t.id "
            "WHERE t.group_id = ? AND m.player_id = ? "
            "AND (t.status = ? OR (t.status = ? AND t.expire_at > ?))"
        )
        params: list = [group_id, player_id, WISH_RECRUITING, WISH_DEPARTED, _utc_now_stamp()]
        if exclude_team_id is not None:
            sql += " AND t.id != ?"
            params.append(exclude_team_id)
        sql += " ORDER BY t.id LIMIT 1"
        async with self._db.execute(sql, tuple(params)) as cursor:
            row = await cursor.fetchone()
        return await self._wish_team_row(row[0]) if row else None

    async def _wish_participated_in_slot(
        self, group_id: str, player_id: str, slot_date: str
    ) -> bool:
        """该玩家是否已在名额日期为 slot_date 的队伍里发过车。

        「同一玩家同一 slot_date 只能参与一次」——周四那支日期有 2 个名额，两支队伍的
        默认队名不同，靠同名状态检查拦不住同一个人先后吃掉两个名额。
        被诸神移出（状态已撤销）之后可以再参加：名额按队伍算，人不再占着它。
        """
        async with self._db.execute(
            "SELECT 1 FROM wish_teams t JOIN wish_team_members m ON m.team_id = t.id "
            "WHERE t.group_id = ? AND m.player_id = ? AND t.slot_date = ? AND t.status = ? "
            "LIMIT 1",
            (group_id, player_id, slot_date, WISH_DEPARTED),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def _wish_grant_status(
        self, group_id: str, player_id: str, team_name: str, expire_at: str
    ) -> None:
        """按队伍的到期时间挂状态，**绝不缩短**已有的更晚状态。

        不走 add_status：后者是 now + days，会给补位进来的人算出一个比队友晚的
        到期时间，也会把一条更晚的同名状态写短。block_actions 与 source 都不在更新
        列表里——阻断项不属于队伍状态，来源更不该被覆盖。
        """
        await self._db.execute(
            "INSERT INTO player_statuses (group_id, player_id, status_name, expire_at, source) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(group_id, player_id, status_name) DO UPDATE SET "
            "expire_at = max(expire_at, excluded.expire_at)",
            (group_id, player_id, team_name, expire_at, STATUS_SOURCE_WISH),
        )

    async def _wish_revoke_status(self, group_id: str, player_id: str, status_name: str) -> int:
        """撤销队伍写进去的那条状态。

        只认 source='wish'：诸神手写的同名状态不归队伍管，名单变化不该动它。
        """
        cursor = await self._db.execute(
            "DELETE FROM player_statuses WHERE group_id = ? AND player_id = ? "
            "AND status_name = ? AND source = ?",
            (group_id, player_id, status_name, STATUS_SOURCE_WISH),
        )
        return cursor.rowcount

    async def _wish_trim_statuses(self, group_id: str, player_id: str, keep: int) -> int:
        """同一玩家最多保留 keep 条队伍状态，超出的按到期时间从早到晚删掉。"""
        if keep <= 0:
            return 0
        async with self._db.execute(
            "SELECT status_name FROM player_statuses "
            "WHERE group_id = ? AND player_id = ? AND source = ? "
            "ORDER BY expire_at DESC, status_name",
            (group_id, player_id, STATUS_SOURCE_WISH),
        ) as cursor:
            names = [r[0] for r in await cursor.fetchall()]
        excess = names[keep:]
        if not excess:
            return 0
        placeholders = ", ".join("?" * len(excess))
        cursor = await self._db.execute(
            f"DELETE FROM player_statuses WHERE group_id = ? AND player_id = ? AND source = ? "
            f"AND status_name IN ({placeholders})",
            (group_id, player_id, STATUS_SOURCE_WISH, *excess),
        )
        return cursor.rowcount

    async def _wish_sync_statuses(self, team: dict, keep: int) -> None:
        """把全队成员的状态同步到队伍的 expire_at（发车与延期共用一条路径）。"""
        for member in await self._wish_members(team["team_id"]):
            await self._wish_grant_status(
                team["group_id"], member["player_id"], team["name"], team["expire_at"]
            )
            await self._wish_trim_statuses(team["group_id"], member["player_id"], keep)

    async def _wish_mark(self, team_id: int, status: str, expect: str) -> bool:
        """条件更新队伍状态，凭 rowcount 认领（并发下只有一个调用者能成功）。"""
        cursor = await self._db.execute(
            "UPDATE wish_teams SET status = ? WHERE id = ? AND status = ?",
            (status, team_id, expect),
        )
        return cursor.rowcount > 0

    async def _wish_promote_leader(self, team_id: int, team: dict) -> None:
        """队长不在了就把最早加入的剩余成员升为队长（队伍必须有人能解散）。"""
        remaining = await self._wish_members(team_id)
        if not remaining:
            return
        new_leader = remaining[0]
        await self._db.execute(
            "UPDATE wish_teams SET leader_id = ?, leader_name = ? WHERE id = ?",
            (new_leader["player_id"], new_leader["player_name"], team_id),
        )

    async def _wish_remove_member_core(
        self, team_id: int, group_id: str, player_id: str
    ) -> Tuple[str, Optional[dict]]:
        """移出成员 + 撤销其状态 + 队长交接 + 空队收尾。调用方负责事务。"""
        team = await self._wish_team_row(team_id)
        if team is None or team["group_id"] != group_id:
            return "not_found", None
        if not await self._wish_is_member(team_id, player_id):
            return "not_member", team

        await self._db.execute(
            "DELETE FROM wish_team_members WHERE team_id = ? AND player_id = ?",
            (team_id, player_id),
        )
        # 招募中的成员本来没有状态，这条 DELETE 是幂等的空操作；已发车的成员则
        # 必须撤——名单驱动，人走了状态就该走
        await self._wish_revoke_status(group_id, player_id, team["name"])

        if not await self._wish_members(team_id):
            await self._wish_mark(team_id, WISH_DISBANDED, team["status"])
        elif team["leader_id"] == player_id:
            await self._wish_promote_leader(team_id, team)
        return "ok", await self._wish_team_with_members(team_id)

    async def _wish_insert_team_with_unique_name(
        self, group_id: str, base_name: str, capacity: int, leader_id: str,
        leader_name: str, slot_date: str, create_date: str,
    ) -> Optional[int]:
        """插队伍行；队名已占用时按序号往后试（队名群内全局唯一）。"""
        for index in range(WISH_NAME_ATTEMPTS):
            name = base_name if index == 0 else f"{base_name}{index + 1}"
            try:
                cursor = await self._db.execute(
                    "INSERT INTO wish_teams "
                    "(group_id, name, capacity, leader_id, leader_name, slot_date, create_date, "
                    " last_reminded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                    (group_id, name, capacity, leader_id, leader_name, slot_date, create_date),
                )
                return cursor.lastrowid
            except aiosqlite.IntegrityError:
                continue
        return None

    # ── 查询 ──

    async def slot_usage(self, group_id: str, slot_date: str) -> int:
        """该名额日期已经发车的队伍数（名额计数的事实来源）。"""
        async with self._db.execute(
            "SELECT COUNT(*) FROM wish_teams WHERE group_id = ? AND slot_date = ? AND status = ?",
            (group_id, slot_date, WISH_DEPARTED),
        ) as cursor:
            return (await cursor.fetchone())[0]

    async def list_wish_teams(
        self, group_id: str, statuses, limit: Optional[int] = None
    ) -> List[dict]:
        """按状态列出队列（含成员）。limit 只截断返回条数，不影响任何计数。"""
        statuses = tuple(statuses)
        if not statuses:
            return []
        placeholders = ", ".join("?" * len(statuses))
        sql = (
            f"SELECT {self._WISH_TEAM_COLUMNS} FROM wish_teams "
            f"WHERE group_id = ? AND status IN ({placeholders}) ORDER BY id"
        )
        params: list = [group_id, *statuses]
        if limit is not None and limit > 0:
            sql += " LIMIT ?"
            params.append(limit)
        async with self._db.execute(sql, tuple(params)) as cursor:
            teams = [self._row_to_wish_team(r) for r in await cursor.fetchall()]
        if not teams:
            return []

        # 成员一次取回，避免每支队查一遍（列表会被大厅/管理列表反复调用）
        ids = [t["team_id"] for t in teams]
        id_placeholders = ", ".join("?" * len(ids))
        async with self._db.execute(
            f"SELECT team_id, player_id, player_name FROM wish_team_members "
            f"WHERE team_id IN ({id_placeholders}) ORDER BY team_id, joined_at, rowid",
            tuple(ids),
        ) as cursor:
            rows = await cursor.fetchall()
        by_team: dict = {team_id: [] for team_id in ids}
        for team_id, player_id, player_name in rows:
            by_team[team_id].append({"player_id": player_id, "player_name": player_name})
        for team in teams:
            team["members"] = by_team[team["team_id"]]
        return teams

    async def get_open_wish_teams(self, group_id: str) -> List[dict]:
        """本群招募中的队伍（含成员）。"""
        return await self.list_wish_teams(group_id, (WISH_RECRUITING,))

    async def wish_team_stats(self, group_id: str, since_date: str) -> dict:
        """近期的运行统计原料：每支队的（状态, 容量, 人数）+ 去重参与人数。

        只取原料、聚合留给服务层：这些比例（发车率、满员率）在 SQL 里算出来
        很难核对，而队伍数量级很小，取回来在 Python 里数更清楚。
        按 `create_date >= since_date` 过滤（北京日期，字符串可直接比大小）。
        """
        async with self._db.execute(
            "SELECT t.status, t.capacity, COUNT(m.player_id) "
            "FROM wish_teams t LEFT JOIN wish_team_members m ON m.team_id = t.id "
            "WHERE t.group_id = ? AND t.create_date >= ? "
            "GROUP BY t.id ORDER BY t.id",
            (group_id, since_date),
        ) as cursor:
            teams = [
                {"status": row[0], "capacity": row[1], "members": row[2]}
                for row in await cursor.fetchall()
            ]

        async with self._db.execute(
            "SELECT COUNT(DISTINCT m.player_id) "
            "FROM wish_teams t JOIN wish_team_members m ON m.team_id = t.id "
            "WHERE t.group_id = ? AND t.create_date >= ?",
            (group_id, since_date),
        ) as cursor:
            players = (await cursor.fetchone())[0]

        return {"teams": teams, "players": players}

    async def get_wish_team(self, team_id: int) -> Optional[dict]:
        """队伍 + 成员。"""
        return await self._wish_team_with_members(team_id)

    async def get_wish_team_by_name(self, group_id: str, name: str) -> Optional[dict]:
        """按队名定位（队名群内全局唯一，所以不会有歧义）。"""
        async with self._db.execute(
            "SELECT id FROM wish_teams WHERE group_id = ? AND name = ?", (group_id, name)
        ) as cursor:
            row = await cursor.fetchone()
        return await self._wish_team_with_members(row[0]) if row else None

    async def get_player_active_wish_team(
        self, group_id: str, player_id: str
    ) -> Optional[dict]:
        """该玩家进行中的队伍（招募中，或窗口未关的已发车队伍）+ 成员。"""
        team = await self._wish_active_team_for_player(group_id, player_id)
        return await self._wish_team_with_members(team["team_id"]) if team else None

    # ── 玩家侧写入 ──

    async def create_wish_team(
        self, group_id: str, base_name: str, capacity: int, leader_id: str,
        leader_name: str, slot_date: str, create_date: str,
        recruiting_limit: int, per_player_limit: int, per_group_limit: int,
    ) -> Tuple[Optional[int], str]:
        """开一支招募中的队伍并让开团者自动入座。返回 (team_id, 结果码)。

        结果码：`ok` / `already_in_team` / `too_many_teams` / `daily_limit` /
        `group_daily_limit` / `name_taken`。

        检查顺序固定，且**任何前置检查失败都不消耗当天名额**：占名额是最后一步，
        否则「本群已有队伍在招募」会白吃掉开团者当天唯一的一次机会。
        """
        async with self.transaction():
            if await self._wish_active_team_for_player(group_id, leader_id):
                return None, "already_in_team"

            if recruiting_limit > 0:
                async with self._db.execute(
                    "SELECT COUNT(*) FROM wish_teams WHERE group_id = ? AND status = ?",
                    (group_id, WISH_RECRUITING),
                ) as cursor:
                    if (await cursor.fetchone())[0] >= recruiting_limit:
                        return None, "too_many_teams"

            if per_player_limit > 0:
                try:
                    await self._db.execute(
                        "INSERT INTO wish_daily_creates (group_id, player_id, create_date) "
                        "VALUES (?, ?, ?)",
                        (group_id, leader_id, create_date),
                    )
                except aiosqlite.IntegrityError:
                    return None, "daily_limit"

            if per_group_limit > 0:
                # 先试 +1（行已存在且未满），再试插入（行还不存在）；
                # 两者都失败说明已达上限
                cursor = await self._db.execute(
                    "UPDATE wish_daily_group_creates SET count = count + 1 "
                    "WHERE group_id = ? AND create_date = ? AND count < ?",
                    (group_id, create_date, per_group_limit),
                )
                if cursor.rowcount == 0:
                    try:
                        await self._db.execute(
                            "INSERT INTO wish_daily_group_creates "
                            "(group_id, create_date, count) VALUES (?, ?, 1)",
                            (group_id, create_date),
                        )
                    except aiosqlite.IntegrityError:
                        return None, "group_daily_limit"

            team_id = await self._wish_insert_team_with_unique_name(
                group_id, base_name, capacity, leader_id, leader_name, slot_date, create_date
            )
            if team_id is None:
                return None, "name_taken"

            await self._db.execute(
                "INSERT INTO wish_team_members (team_id, group_id, player_id, player_name) "
                "VALUES (?, ?, ?, ?)",
                (team_id, group_id, leader_id, leader_name),
            )
            return team_id, "ok"

    async def join_wish_team(
        self, team_id: int, group_id: str, player_id: str, player_name: str,
        slot_limit: int, status_days: int, keep_statuses: int,
    ) -> Tuple[str, Optional[dict]]:
        """加入招募中的队伍，或补位已发车队伍的空位。返回 (结果码, 队伍含成员)。

        结果码：
          `joined`            加入招募中的队伍（还没满员）
          `departed`          这次加入正好满员且抢到名额 → 已给全员挂状态
          `replenished`       补位到已发车队伍 → 已给本人按队伍到期时间挂状态
          `already_member`    已经是成员（重复点「祈愿加入」不该报错）
          `not_found` / `window_closed` / `voided` / `disbanded` / `full` /
          `name_conflict` / `already_in_slot` / `already_in_other_team` / `no_slot`
        """
        async with self.transaction():
            team = await self._wish_team_row(team_id)
            if team is None or team["group_id"] != group_id:
                return "not_found", None

            status = team["status"]
            if status == WISH_DEPARTED:
                if not self._wish_window_open(team):
                    return "window_closed", await self._wish_team_with_members(team_id)
            elif status != WISH_RECRUITING:
                return status, await self._wish_team_with_members(team_id)

            if await self._wish_is_member(team_id, player_id):
                return "already_member", await self._wish_team_with_members(team_id)

            if await self._wish_status_expiry(group_id, player_id, team["name"]):
                return "name_conflict", None
            if await self._wish_participated_in_slot(group_id, player_id, team["slot_date"]):
                return "already_in_slot", None
            if await self._wish_active_team_for_player(
                group_id, player_id, exclude_team_id=team_id
            ):
                return "already_in_other_team", None

            members = await self._wish_members(team_id)
            if len(members) >= team["capacity"]:
                return "full", await self._wish_team_with_members(team_id)

            if status == WISH_RECRUITING and slot_limit > 0:
                # 名额已用尽时直接劝退，别让人白等 45 分钟
                if await self.slot_usage(group_id, team["slot_date"]) >= slot_limit:
                    return "no_slot", await self._wish_team_with_members(team_id)

            await self._db.execute(
                "INSERT INTO wish_team_members (team_id, group_id, player_id, player_name) "
                "VALUES (?, ?, ?, ?)",
                (team_id, group_id, player_id, player_name),
            )

            if status == WISH_DEPARTED:
                # 补位：按队伍的到期时间挂上，与队友同一天到期
                await self._wish_grant_status(
                    group_id, player_id, team["name"], team["expire_at"]
                )
                await self._wish_trim_statuses(group_id, player_id, keep_statuses)
                return "replenished", await self._wish_team_with_members(team_id)

            if len(members) + 1 < team["capacity"]:
                return "joined", await self._wish_team_with_members(team_id)

            # 正好满员 → 定下队伍的到期时间，并给全队挂状态
            expire_at = _utc_stamp_after(status_days)
            await self._db.execute(
                "UPDATE wish_teams SET status = ?, departed_at = ?, expire_at = ? WHERE id = ?",
                (WISH_DEPARTED, _utc_now_stamp(), expire_at, team_id),
            )
            team["status"] = WISH_DEPARTED
            team["expire_at"] = expire_at
            await self._wish_sync_statuses(team, keep_statuses)
            return "departed", await self._wish_team_with_members(team_id)

    async def leave_wish_team(
        self, group_id: str, player_id: str
    ) -> Tuple[str, Optional[dict]]:
        """退出自己所在的招募中队伍。

        队长退出 = 解散（队伍不能没有队长）；已发车队伍不接受成员自行退出——
        3 天窗口内的名单变更走诸神的移出/换人。
        结果码：`disbanded` / `left` / `not_found`。
        """
        async with self.transaction():
            team = await self._wish_active_team_for_player(group_id, player_id)
            if team is None or team["status"] != WISH_RECRUITING:
                return "not_found", None

            if team["leader_id"] == player_id:
                await self._wish_mark(team["team_id"], WISH_DISBANDED, WISH_RECRUITING)
                return "disbanded", await self._wish_team_with_members(team["team_id"])

            code, updated = await self._wish_remove_member_core(
                team["team_id"], group_id, player_id
            )
            return ("left" if code == "ok" else code), updated

    # ── 诸神侧写入 ──

    async def remove_wish_team_member(
        self, team_id: int, group_id: str, player_id: str
    ) -> Tuple[str, Optional[dict]]:
        """把成员移出队伍（不受队长限制），并撤销他因这支队拿到的状态。

        移出的是队长则把最早加入的剩余成员升为队长；队伍空了则解散。
        结果码：`ok` / `not_found` / `not_member`。
        """
        async with self.transaction():
            return await self._wish_remove_member_core(team_id, group_id, player_id)

    async def swap_wish_team_member(
        self, team_id: int, group_id: str, out_player_id: str, in_player_id: str,
        in_player_name: str, keep_statuses: int,
    ) -> Tuple[str, Optional[dict]]:
        """诸神换人：把 in 换进来、out 换出去，并把「房间状态」转移过去。

        转移 = 撤销 out 名下那条队伍状态 + 按**队伍的 expire_at** 给 in 挂上同样的
        状态名与到期时间。以名单为准：即使 out 身上已经没有那条状态（比如被
        「移除状态」清过），照样换人。
        结果码：`ok` / `not_found` / `same_player` / `not_member` / `already_member` /
        `in_has_team` / `in_in_slot` / `in_name_conflict`。
        """
        async with self.transaction():
            team = await self._wish_team_row(team_id)
            if team is None or team["group_id"] != group_id:
                return "not_found", None
            if out_player_id == in_player_id:
                return "same_player", None
            if not await self._wish_is_member(team_id, out_player_id):
                return "not_member", await self._wish_team_with_members(team_id)
            if await self._wish_is_member(team_id, in_player_id):
                return "already_member", await self._wish_team_with_members(team_id)
            if await self._wish_active_team_for_player(group_id, in_player_id):
                return "in_has_team", None
            if await self._wish_participated_in_slot(group_id, in_player_id, team["slot_date"]):
                return "in_in_slot", None
            if await self._wish_status_expiry(group_id, in_player_id, team["name"]):
                return "in_name_conflict", None

            await self._db.execute(
                "DELETE FROM wish_team_members WHERE team_id = ? AND player_id = ?",
                (team_id, out_player_id),
            )
            await self._db.execute(
                "INSERT INTO wish_team_members (team_id, group_id, player_id, player_name) "
                "VALUES (?, ?, ?, ?)",
                (team_id, group_id, in_player_id, in_player_name),
            )
            await self._wish_revoke_status(group_id, out_player_id, team["name"])
            if team["leader_id"] == out_player_id:
                await self._db.execute(
                    "UPDATE wish_teams SET leader_id = ?, leader_name = ? WHERE id = ?",
                    (in_player_id, in_player_name, team_id),
                )
            if team["status"] == WISH_DEPARTED and self._wish_window_open(team):
                await self._wish_grant_status(
                    group_id, in_player_id, team["name"], team["expire_at"]
                )
                await self._wish_trim_statuses(group_id, in_player_id, keep_statuses)
            return "ok", await self._wish_team_with_members(team_id)

    async def extend_wish_team_expiry(
        self, team_id: int, group_id: str, days: int, keep_statuses: int
    ) -> Tuple[str, Optional[dict]]:
        """整队延期：改队伍的 expire_at，再把全队状态同步到新值。

        改的是**队伍**的到期时间，不是每人各自 +N 天——名单可变之后各自加会让队友
        之间的到期时间漂开。只延不缩：days <= 0 直接拒绝。
        结果码：`ok` / `not_found` / `invalid_days` / `not_departed`。
        """
        if days <= 0:
            return "invalid_days", None
        async with self.transaction():
            team = await self._wish_team_row(team_id)
            if team is None or team["group_id"] != group_id:
                return "not_found", None
            if team["status"] != WISH_DEPARTED or not team["expire_at"]:
                return "not_departed", await self._wish_team_with_members(team_id)

            new_expiry = (
                datetime.strptime(team["expire_at"], "%Y-%m-%d %H:%M:%S")
                + timedelta(days=days)
            ).strftime("%Y-%m-%d %H:%M:%S")
            await self._db.execute(
                "UPDATE wish_teams SET expire_at = ? WHERE id = ?", (new_expiry, team_id)
            )
            team["expire_at"] = new_expiry
            await self._wish_sync_statuses(team, keep_statuses)
            return "ok", await self._wish_team_with_members(team_id)

    async def rename_wish_team(
        self, team_id: int, group_id: str, new_name: str
    ) -> Tuple[str, dict]:
        """给队伍改名（诸神是唯一的命名途径），已发车的队伍连带改成员状态名。

        状态名 = 队名，所以改名必须同步到成员身上，否则会留下「队名换了、状态还挂
        旧名」的错位。改的是状态名本身（保留 expire_at / block_actions / source），
        不是删了重挂——重挂会洗掉剩余期限。改名不动 slot_date，所以不影响名额与
        星期规则（名额按 slot_date 算，从不解析队名）。
        结果码：`ok` / `not_found` / `name_taken` / `empty_name`。
        返回的详情：{"renamed": n, "merged": m, "skipped": k}。
        """
        detail = {"renamed": 0, "merged": 0, "skipped": 0}
        new_name = (new_name or "").strip()
        if not new_name:
            return "empty_name", detail

        async with self.transaction():
            team = await self._wish_team_row(team_id)
            if team is None or team["group_id"] != group_id:
                return "not_found", detail
            old_name = team["name"]
            if old_name == new_name:
                return "ok", detail

            try:
                await self._db.execute(
                    "UPDATE wish_teams SET name = ? WHERE id = ?", (new_name, team_id)
                )
            except aiosqlite.IntegrityError:
                return "name_taken", detail

            for member in await self._wish_members(team_id):
                result = await self._rename_status_core(
                    group_id, member["player_id"], old_name, new_name,
                    source_filter=STATUS_SOURCE_WISH,
                )
                if result == "not_found":
                    detail["skipped"] += 1
                elif result == "merged":
                    detail["merged"] += 1
                    detail["renamed"] += 1
                else:
                    detail["renamed"] += 1
            return "ok", detail

    async def disband_wish_team(
        self, team_id: int, group_id: str
    ) -> Tuple[str, Optional[dict]]:
        """解散队伍；已发车的队伍连带撤销全队状态。

        为什么撤状态：名单驱动——队伍没了，名单上的人就不该再挂着这支队的状态。
        （只解散招募中的队伍时没有状态可撤。）
        结果码：`ok` / `not_found` / `not_active`。
        """
        async with self.transaction():
            team = await self._wish_team_with_members(team_id)
            if team is None or team["group_id"] != group_id:
                return "not_found", None
            if not await self._wish_mark(team_id, WISH_DISBANDED, team["status"]):
                return "not_active", team

            was_departed = team["status"] == WISH_DEPARTED
            team["status"] = WISH_DISBANDED
            if was_departed:
                for member in team["members"]:
                    await self._wish_revoke_status(group_id, member["player_id"], team["name"])
            return "ok", team

    # ── 调度与清理 ──

    async def claim_wish_reminder(self, team_id: int, cutoff: str) -> bool:
        """认领这次提醒，凭 rowcount 判定（写法同 paika 的 claim_reminder）。

        条件更新 + rowcount 意味着并发 tick 下同一支队伍在一个提醒窗口内只会被提醒
        一次；last_reminded_at 落在表里，插件重启后间隔照常算。
        """
        cursor = await self._db.execute(
            "UPDATE wish_teams SET last_reminded_at = ? WHERE id = ? AND status = ? "
            "AND (last_reminded_at IS NULL OR last_reminded_at <= ?)",
            (_utc_now_stamp(), team_id, WISH_RECRUITING, cutoff),
        )
        await self._db.commit()
        return cursor.rowcount == 1

    async def disband_stale_wish_teams(self, cutoff: str) -> List[dict]:
        """解散超时未发车的队伍，返回被解散的队伍与成员（供播报）。

        按 created_at 扫库而不是内存计时：插件离线期间到期的队伍，下次启动的第一次
        tick 就会清掉，不会永久卡在招募中。
        """
        async with self.transaction():
            async with self._db.execute(
                "SELECT id FROM wish_teams WHERE status = ? AND created_at <= ? ORDER BY id",
                (WISH_RECRUITING, cutoff),
            ) as cursor:
                ids = [r[0] for r in await cursor.fetchall()]

            affected = []
            for team_id in ids:
                if not await self._wish_mark(team_id, WISH_DISBANDED, WISH_RECRUITING):
                    continue  # 已被别处解散
                team = await self._wish_team_with_members(team_id)
                if team:
                    affected.append(team)
            return affected

    async def void_sibling_teams(self, group_id: str, slot_date: str) -> List[dict]:
        """把同一名额日期里其余还在招募的队伍判为「未能成行」。

        某队发车后该日期名额若已用尽，剩下的队伍即使凑满也发不了车；让它们挂着等
        45 分钟超时（成员还蒙在鼓里）比直接宣判更糟。返回被宣判的队伍（供播报）。
        """
        async with self.transaction():
            async with self._db.execute(
                "SELECT id FROM wish_teams WHERE group_id = ? AND slot_date = ? AND status = ?",
                (group_id, slot_date, WISH_RECRUITING),
            ) as cursor:
                ids = [r[0] for r in await cursor.fetchall()]

            affected = []
            for team_id in ids:
                if not await self._wish_mark(team_id, WISH_VOIDED, WISH_RECRUITING):
                    continue
                team = await self._wish_team_with_members(team_id)
                if team:
                    affected.append(team)
            return affected

    async def purge_old_wish_teams(self, days: int) -> int:
        """删除超过保留期的历史队伍记录（招募中的不删）。返回删除的队伍数。

        只删记录：成员的队伍状态有自己的到期时间，早该自然过期了。
        """
        cutoff = _utc_stamp_after(-max(1, int(days)))
        async with self.transaction():
            async with self._db.execute(
                "SELECT id FROM wish_teams WHERE status != ? AND created_at <= ?",
                (WISH_RECRUITING, cutoff),
            ) as cursor:
                ids = [r[0] for r in await cursor.fetchall()]
            if not ids:
                return 0
            placeholders = ", ".join("?" * len(ids))
            await self._db.execute(
                f"DELETE FROM wish_team_members WHERE team_id IN ({placeholders})",
                tuple(ids),
            )
            await self._db.execute(
                f"DELETE FROM wish_teams WHERE id IN ({placeholders})", tuple(ids)
            )
            return len(ids)

    async def clear_wish_teams(self, group_id: str) -> int:
        """清空本群的队伍与成员记录。返回删除的队伍数。

        副作用是真接得到的：名额由 slot_usage（数 departed 行）算出，所以删掉当天
        已发车的行就等于**释放当天名额**——「清空」作为「今天重来」的逃生口不需要
        额外逻辑。已经发出去的状态不在这里撤销（要撤用「移除状态」）。
        """
        async with self.transaction():
            async with self._db.execute(
                "SELECT COUNT(*) FROM wish_teams WHERE group_id = ?", (group_id,)
            ) as cursor:
                count = (await cursor.fetchone())[0]
            await self._db.execute(
                "DELETE FROM wish_team_members WHERE group_id = ?", (group_id,)
            )
            await self._db.execute("DELETE FROM wish_teams WHERE group_id = ?", (group_id,))
            return count


def _serialized(fn):
    """把对外方法包进 `_write_guard`：调用期间独占数据库连接。

    可重入——同一个任务里嵌套调用（`transaction()` 里再调写方法、写方法内部
    再查询）不会自我阻塞。
    """

    @functools.wraps(fn)
    async def wrapper(self, *args, **kwargs):
        async with self._write_guard():
            return await fn(self, *args, **kwargs)

    return wrapper


# 需要独立连接的备份方法独占不了 shared connection，且耗时可观（VACUUM INTO）：
# 把它挡在闸门外，免得每天备份时把所有指令一起卡住。
_SERIALIZE_EXEMPT = {"backup_to"}

# 逐个手工标注容易漏（72 个方法里 48 个会写库），所以按签名统一包装：
# 所有对外的 async 方法都走闸门，读也一样——单连接 SQLite 上没有"并发读"，
# 少一个分类就少一次"哪几个方法忘了加锁"的排查。
for _name, _member in list(vars(DatabaseManager).items()):
    if _name.startswith("_") or _name in _SERIALIZE_EXEMPT:
        continue
    if not inspect.iscoroutinefunction(_member):
        continue
    setattr(DatabaseManager, _name, _serialized(_member))
del _name, _member
