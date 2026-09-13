"""
代码审查修复项的回归测试。

每个用例对应一处此前实际存在的缺陷，说明写在各自 docstring 里，
以免以后有人"顺手简化"又把问题改回去。
"""

import asyncio
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from astrbot_plugin_faith_ladder.db_manager import DatabaseManager
from astrbot_plugin_faith_ladder.ladder_service import LadderService


@pytest.fixture
async def db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = DatabaseManager(Path(tmpdir))
        await db.initialize()
        yield db
        await db.close()


class TestStatusTimezone:
    """状态表的时间基准必须全程用 UTC（写入时就是 UTC）。"""

    async def test_remaining_days_is_one_for_fresh_day_status(self, db):
        await db.upsert_player("g1", "u1", "Alice")
        await db.add_status("g1", "u1", "虚弱", 1)
        await db.commit()
        statuses = await db.get_player_statuses("g1", "u1")
        assert len(statuses) == 1
        assert statuses[0]["remaining_days"] == 1

    async def test_status_not_filtered_out_early(self, db):
        """剩余不足 8 小时的状态也不该消失。

        此前写入用 UTC、查询用 UTC+8，状态会提前 8 小时被过滤掉，
        而 _calc_remaining_days 又拿 naive 值与 aware 值相减，批量查询直接抛 TypeError。
        """
        await db.upsert_player("g1", "u1", "Alice")
        await db.add_status("g1", "u1", "虚弱", 1)
        await db.commit()
        # 把到期时间改到 7 小时后（UTC），仍应可见
        soon = (datetime.now(timezone.utc) + timedelta(hours=7)).strftime("%Y-%m-%d %H:%M:%S")
        await db._db.execute(
            "UPDATE player_statuses SET expire_at = ? WHERE group_id = 'g1'", (soon,)
        )
        await db.commit()
        assert len(await db.get_player_statuses("g1", "u1")) == 1

    async def test_batch_status_query_does_not_crash(self, db):
        """批量查询取状态时不应抛异常（此前 naive/aware 相减必然 TypeError）。"""
        await db.upsert_player("g1", "u1", "Alice")
        await db.add_status("g1", "u1", "虚弱", 3)
        await db.commit()
        result = await db.get_statuses_for_players("g1", ["u1"])
        assert result[0][0] == "u1"
        assert result[0][1][0]["status_name"] == "虚弱"

    async def test_purge_is_committed(self, db):
        """过期状态清理必须落盘：调度器随后直接调用它，没有外层事务。

        此前不 commit，删除只存在于未提交事务里，连接一关就复活。
        """
        await db.upsert_player("g1", "u1", "Alice")
        await db.add_status("g1", "u1", "虚弱", 1)
        await db.commit()
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        await db._db.execute(
            "UPDATE player_statuses SET expire_at = ? WHERE group_id = 'g1'", (past,)
        )
        await db.commit()

        purged = await db.purge_expired_statuses()
        assert purged == 1
        # 重开连接确认真的删掉了
        data_dir = db.data_dir
        await db.close()
        db2 = DatabaseManager(data_dir)
        await db2.initialize()
        async with db2._db.execute("SELECT COUNT(*) FROM player_statuses") as cur:
            assert (await cur.fetchone())[0] == 0
        await db2.close()


class TestItemQuantityValidation:
    """道具数量必须为正：负数量会让 SQL 里的减法变成加法（凭空造道具）。"""

    @pytest.fixture
    async def service(self, db):
        return LadderService(db)

    async def test_deduct_item_rejects_negative(self, service):
        await service.db.upsert_player("g1", "u1", "Alice")
        await service.db.add_item("g1", "u1", "铁剑", 1)
        ok, _, _, _ = await service.deduct_item("g1", "u1", "Alice", "铁剑", -1)
        assert ok is False
        items = await service.db.get_player_items("g1", "u1")
        assert items[0]["quantity"] == 1  # 没有被"扣"成 2

    async def test_give_items_rejects_non_positive(self, service):
        await service.db.upsert_player("g1", "u1", "Alice")
        ok, _ = await service.give_items("g1", "Alice", [("铁剑", -5)])
        assert ok is False
        assert await service.db.get_player_items("g1", "u1") == []

    def test_parse_item_args_rejects_non_positive(self):
        from astrbot_plugin_faith_ladder.item_utils import parse_item_args

        with pytest.raises(ValueError):
            parse_item_args("铁剑 -1")
        with pytest.raises(ValueError):
            parse_item_args("铁剑*0")
        with pytest.raises(ValueError):
            parse_item_args("铁剑 -3 药水 2")

    def test_parse_item_args_accepts_both_quantity_forms(self):
        from astrbot_plugin_faith_ladder.item_utils import parse_item_args

        assert parse_item_args("测试*10（b）") == [("测试（b）", 10)]
        assert parse_item_args("测试（b）*10") == [("测试（b）", 10)]
        assert parse_item_args("铁剑 2 药水*3") == [("铁剑", 2), ("药水", 3)]


class TestPlayerDeletionCascade:
    """删除玩家/清空群数据时必须同时清掉依赖玩家 ID 的附属表。"""

    async def test_delete_player_cleans_daily_tables(self, db):
        await db.upsert_player("g1", "u1", "Alice")
        await db.record_prayer_hit("g1", "u1", 0)
        await db.record_gift_accept("g1", "u1")
        await db.save_pending_gift(
            "g1", "u1", "u2", "Bob", "Alice", '{"item_name": "铁剑", "grade": null, "quantity": 1}'
        )

        assert await db.delete_player("g1", "u1") is True

        assert await db.has_prayer_hit_today("g1", "u1") is False
        assert await db.count_gift_accepts_today("g1", "u1") == 0
        assert await db.get_pending_gift("g1", "u1") is None

    async def test_delete_all_players_cleans_daily_tables(self, db):
        await db.upsert_player("g1", "u1", "Alice")
        await db.record_prayer_hit("g1", "u1", 0)
        await db.record_gift_accept("g1", "u1")

        await db.delete_all_players("g1")

        assert await db.has_prayer_hit_today("g1", "u1") is False
        assert await db.count_gift_accepts_today("g1", "u1") == 0


class TestRenameConflict:
    """改名必须拒绝重名，且并发下也不能产生两个同名玩家。"""

    async def test_rename_rejects_existing_name(self, db):
        await db.upsert_player("g1", "u1", "Alice")
        await db.upsert_player("g1", "u2", "Bob")
        ok, msg = await db.rename_player_by_name("g1", "Alice", "Bob")
        assert ok is False
        assert "已存在" in msg
        assert (await db.get_player_by_name("g1", "Alice")) is not None

    async def test_rename_succeeds_when_free(self, db):
        await db.upsert_player("g1", "u1", "Alice")
        ok, _ = await db.rename_player_by_name("g1", "Alice", "Carol")
        assert ok is True
        assert (await db.get_player_by_name("g1", "Carol")) is not None

    async def test_concurrent_rename_to_same_name_only_one_wins(self, db):
        await db.upsert_player("g1", "u1", "Alice")
        await db.upsert_player("g1", "u2", "Bob")

        results = await asyncio.gather(
            db.rename_player_by_name("g1", "Alice", "Carol"),
            db.rename_player_by_name("g1", "Bob", "Carol"),
        )
        assert sum(1 for ok, _ in results if ok) == 1
        players = await db.get_all_players_in_group("g1")
        assert sum(1 for p in players if p.player_name == "Carol") == 1


class TestPerGradeInventory:
    """同名不同等级的道具必须是各自独立的行。

    旧主键 (group, player, name) 不含 grade，导致「铁剑」和「铁剑（A级）」
    只能存在一行：后者并入前者后等级丢失，之后按等级赠送/扣除都会报"没有道具"。
    """

    @pytest.fixture
    async def service(self, db):
        return LadderService(db)

    async def test_same_name_different_grades_are_separate_rows(self, db):
        await db.upsert_player("g1", "u1", "Alice")
        await db.add_item("g1", "u1", "铁剑", 1, grade=None)
        await db.add_item("g1", "u1", "铁剑", 2, grade="A")
        await db.commit()

        items = await db.get_player_items("g1", "u1")
        assert {i["grade"]: i["quantity"] for i in items} == {None: 1, "A": 2}

    async def test_add_same_grade_accumulates(self, db):
        await db.upsert_player("g1", "u1", "Alice")
        await db.add_item("g1", "u1", "铁剑", 1, grade="A")
        await db.add_item("g1", "u1", "铁剑", 3, grade="A")
        await db.commit()
        items = await db.get_player_items("g1", "u1")
        assert len(items) == 1
        assert items[0]["quantity"] == 4

    async def test_nonstandard_grade_roundtrip(self, db):
        """有括号但非标准等级（如（D））读回仍是 ''，且与"无等级"是两行。"""
        await db.upsert_player("g1", "u1", "Alice")
        await db.add_item("g1", "u1", "淬锋砺剑", 1, grade="")
        await db.commit()
        items = await db.get_player_items("g1", "u1")
        assert items[0]["grade"] == ""

        await db.add_item("g1", "u1", "淬锋砺剑", 1, grade=None)
        await db.commit()
        assert len(await db.get_player_items("g1", "u1")) == 2

    async def test_deduct_graded_item_while_holding_plain_one(self, service):
        """给持有无等级版本的人发带等级的道具，仍能按等级扣出来（修复的核心场景）。"""
        await service.db.upsert_player("g1", "u1", "Alice")
        await service.db.add_item("g1", "u1", "铁剑", 1, grade=None)
        await service.db.add_item("g1", "u1", "铁剑", 2, grade="A")
        await service.db.commit()

        ok, msg, base, grade = await service.deduct_item("g1", "u1", "Alice", "铁剑（A级）", 2)
        assert ok is True
        assert (base, grade) == ("铁剑", "A")

        items = await service.db.get_player_items("g1", "u1")
        assert {i["grade"]: i["quantity"] for i in items} == {None: 1}

    async def test_remove_item_scoped_to_grade(self, db):
        await db.upsert_player("g1", "u1", "Alice")
        await db.add_item("g1", "u1", "铁剑", 5, grade=None)
        await db.add_item("g1", "u1", "铁剑", 5, grade="A")
        await db.commit()

        assert await db.remove_item("g1", "u1", "铁剑", 2, grade="A") is True
        items = {i["grade"]: i["quantity"] for i in await db.get_player_items("g1", "u1")}
        assert items == {None: 5, "A": 3}

    async def test_remove_item_without_grade_targets_plain_row(self, db):
        """不指定等级且只扣数量时，只作用于「无等级」那一行。"""
        await db.upsert_player("g1", "u1", "Alice")
        await db.add_item("g1", "u1", "铁剑", 5, grade=None)
        await db.add_item("g1", "u1", "铁剑", 5, grade="A")
        await db.commit()

        assert await db.remove_item("g1", "u1", "铁剑", 2) is True
        items = {i["grade"]: i["quantity"] for i in await db.get_player_items("g1", "u1")}
        assert items == {None: 3, "A": 5}

    async def test_remove_item_without_grade_keeps_other_grades(self, db):
        """grade=None 只作用于「无等级」那一行，不碰同名其它等级。

        这条语义在本轮被纠正：此前 grade=None 且 quantity=None 表示"删除所有等级"，
        于是「收回道具 张三 铁剑」会连 A 级一起删掉却只报无等级那一行的数量。
        需要清空某道具全部等级时用 clear_items（清除储物空间 <玩家> <道具名>）。
        """
        await db.upsert_player("g1", "u1", "Alice")
        await db.add_item("g1", "u1", "铁剑", 5, grade=None)
        await db.add_item("g1", "u1", "铁剑", 5, grade="A")
        await db.commit()

        assert await db.remove_item("g1", "u1", "铁剑") is True
        items = {i["grade"]: i["quantity"] for i in await db.get_player_items("g1", "u1")}
        assert items == {"A": 5}

    async def test_clear_items_removes_all_grades(self, db):
        """清空某道具（clear_items）才是"所有等级一起清"的入口。"""
        await db.upsert_player("g1", "u1", "Alice")
        await db.add_item("g1", "u1", "铁剑", 5, grade=None)
        await db.add_item("g1", "u1", "铁剑", 5, grade="A")
        await db.commit()

        assert await db.clear_items("g1", "u1", "铁剑") == 2
        assert await db.get_player_items("g1", "u1") == []


class TestPickItemRow:
    """未指定等级时的选行规则：优先无等级行，否则取等级最低的一行。

    此前 deduct_item 直接取列表首项，而 get_player_items 按等级降序返回，
    于是「赠送道具 Bob 铁剑」会优先扣掉铁剑（A级）。
    """

    @pytest.fixture
    async def service(self, db):
        return LadderService(db)

    async def test_deduct_prefers_no_grade_row(self, service):
        await service.db.upsert_player("g1", "u1", "Alice")
        await service.db.add_item("g1", "u1", "铁剑", 2, grade=None)
        await service.db.add_item("g1", "u1", "铁剑", 9, grade="A")
        await service.db.commit()

        ok, _, base, grade = await service.deduct_item("g1", "u1", "Alice", "铁剑", 1)
        assert ok is True
        assert grade is None  # 扣的是无等级那一行，不是 A 级

        items = {i["grade"]: i["quantity"] for i in await service.db.get_player_items("g1", "u1")}
        assert items == {None: 1, "A": 9}

    async def test_deduct_falls_back_to_lowest_grade(self, service):
        """没有无等级行时取等级最低的一行（C 级低于 A 级）。"""
        await service.db.upsert_player("g1", "u1", "Alice")
        await service.db.add_item("g1", "u1", "铁剑", 3, grade="A")
        await service.db.add_item("g1", "u1", "铁剑", 4, grade="C")
        await service.db.commit()

        ok, _, _, grade = await service.deduct_item("g1", "u1", "Alice", "铁剑", 1)
        assert ok is True
        assert grade == "C"

    async def test_deduct_explicit_grade_is_exact(self, service):
        await service.db.upsert_player("g1", "u1", "Alice")
        await service.db.add_item("g1", "u1", "铁剑", 2, grade=None)
        await service.db.add_item("g1", "u1", "铁剑", 3, grade="A")
        await service.db.commit()

        ok, _, _, grade = await service.deduct_item("g1", "u1", "Alice", "铁剑（A级）", 2)
        assert ok is True
        assert grade == "A"
        items = {i["grade"]: i["quantity"] for i in await service.db.get_player_items("g1", "u1")}
        assert items == {None: 2, "A": 1}

    async def test_take_all_keeps_other_grades_and_hints(self, service):
        """「收回道具 张三 铁剑」（全部收回）只收回无等级行，并提示还有其它等级。"""
        await service.db.upsert_player("g1", "Alice", "Alice")
        await service.db.add_item("g1", "Alice", "铁剑", 2, grade=None)
        await service.db.add_item("g1", "Alice", "铁剑", 5, grade="A")
        await service.db.commit()

        ok, msg = await service.take_items("g1", "Alice", [("铁剑", None)])
        assert ok is True
        items = {i["grade"]: i["quantity"] for i in await service.db.get_player_items("g1", "Alice")}
        assert items == {"A": 5}          # A 级完好
        assert "另有" in msg and "A级" in msg.replace(" ", "")


class TestGradePrimaryKeyMigration:
    """旧库（主键不含 grade、grade 为 NULL）升级后数据保留，且主键已含 grade。"""

    async def test_old_schema_is_migrated(self, tmp_path):
        import sqlite3

        db_path = tmp_path / "ladder.db"
        con = sqlite3.connect(db_path)
        con.executescript("""
            CREATE TABLE players (
                player_id TEXT NOT NULL, group_id TEXT NOT NULL, player_name TEXT NOT NULL,
                class TEXT, faith TEXT, specific_faith TEXT,
                ladder_score INTEGER DEFAULT 0, pilgrimage_score INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                oathbreaker INTEGER DEFAULT 0, qq_id TEXT,
                PRIMARY KEY (player_id, group_id)
            );
            CREATE TABLE player_items (
                group_id TEXT NOT NULL, player_id TEXT NOT NULL, item_name TEXT NOT NULL,
                grade TEXT DEFAULT NULL, quantity INTEGER DEFAULT 1,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (group_id, player_id, item_name)
            );
            INSERT INTO players (player_id, group_id, player_name) VALUES ('u1', 'g1', 'Alice');
            INSERT INTO player_items (group_id, player_id, item_name, grade, quantity)
                VALUES ('g1', 'u1', '铁剑', NULL, 7);
        """)
        con.commit()
        con.close()

        db = DatabaseManager(tmp_path)
        await db.initialize()
        try:
            items = await db.get_player_items("g1", "u1")
            assert len(items) == 1
            assert items[0]["item_name"] == "铁剑"
            assert items[0]["quantity"] == 7
            assert items[0]["grade"] is None  # NULL 归一化为"无等级"

            # 主键已含 grade：同名不同等级可以共存
            await db.add_item("g1", "u1", "铁剑", 1, grade="A")
            await db.commit()
            assert len(await db.get_player_items("g1", "u1")) == 2
        finally:
            await db.close()

    @staticmethod
    def _make_old_schema_db(db_path):
        """造一个旧结构的库：player_items 主键不含 grade，grade 允许 NULL。"""
        import sqlite3

        con = sqlite3.connect(db_path)
        con.executescript("""
            CREATE TABLE players (
                player_id TEXT NOT NULL, group_id TEXT NOT NULL, player_name TEXT NOT NULL,
                class TEXT, faith TEXT, specific_faith TEXT,
                ladder_score INTEGER DEFAULT 0, pilgrimage_score INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                oathbreaker INTEGER DEFAULT 0, qq_id TEXT,
                PRIMARY KEY (player_id, group_id)
            );
            CREATE TABLE player_items (
                group_id TEXT NOT NULL, player_id TEXT NOT NULL, item_name TEXT NOT NULL,
                grade TEXT DEFAULT NULL, quantity INTEGER DEFAULT 1,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (group_id, player_id, item_name)
            );
            INSERT INTO players (player_id, group_id, player_name) VALUES ('u1', 'g1', 'Alice');
            INSERT INTO player_items (group_id, player_id, item_name, grade, quantity) VALUES
                ('g1', 'u1', '铁剑', NULL, 2),
                ('g1', 'u1', '盾牌', 'A', 3);
        """)
        con.commit()
        con.close()

    async def test_rebuild_rolls_back_when_interrupted(self, tmp_path, monkeypatch):
        """重建中途失败必须整体回滚：表与数据都还在。

        旧实现用 executescript（先隐式提交、且不把脚本包在事务里），
        DROP 之后再失败就会永久丢表。
        """
        import aiosqlite

        db_path = tmp_path / "ladder.db"
        self._make_old_schema_db(db_path)
        db = DatabaseManager(tmp_path)

        orig_execute = aiosqlite.Connection.execute

        def failing_execute(self, sql, parameters=None):
            if "RENAME TO player_items" in sql:
                raise RuntimeError("注入的失败")
            return orig_execute(self, sql, parameters)

        monkeypatch.setattr(aiosqlite.Connection, "execute", failing_execute)
        await db.initialize()          # 迁移失败由内部捕获并回滚
        monkeypatch.undo()

        # 关键不变量：真正的 DROP 在事务内，已回滚，数据必须完好
        items = await db.get_player_items("g1", "u1")
        assert {i["item_name"] for i in items} == {"铁剑", "盾牌"}
        await db.close()

        # 再启动一次（模拟重启）：应自愈——清掉残留的空暂存表并完成重建
        db2 = DatabaseManager(db_path.parent)
        await db2.initialize()
        try:
            items = await db2.get_player_items("g1", "u1")
            assert {i["item_name"] for i in items} == {"铁剑", "盾牌"}
            assert not await db2._table_exists("player_items_new")
            # 主键已含 grade：同名不同等级可共存
            await db2.add_item("g1", "u1", "铁剑", 1, grade="A")
            await db2.commit()
            assert len(await db2.get_player_items("g1", "u1")) == 3
        finally:
            await db2.close()

    async def test_recovers_when_rebuild_was_interrupted(self, tmp_path):
        """模拟「DROP 成功、RENAME 未执行」后再启动：暂存表的数据必须回到 player_items。

        这是最隐蔽的一种中断：_create_tables 会用新结构重建一张空的 player_items，
        主键判断因此认为"已迁移完成"，暂存表里的道具将永远不可见。
        """
        import sqlite3

        db_path = tmp_path / "ladder.db"
        self._make_old_schema_db(db_path)
        con = sqlite3.connect(db_path)
        con.executescript("""
            CREATE TABLE player_items_new (
                group_id TEXT NOT NULL, player_id TEXT NOT NULL, item_name TEXT NOT NULL,
                grade TEXT NOT NULL DEFAULT '', quantity INTEGER DEFAULT 1,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (group_id, player_id, item_name, grade)
            );
            INSERT INTO player_items_new (group_id, player_id, item_name, grade, quantity, updated_at)
                SELECT group_id, player_id, item_name,
                       CASE WHEN grade IS NULL THEN '' ELSE grade END, quantity, updated_at
                FROM player_items;
            DROP TABLE player_items;
        """)
        con.commit()
        con.close()

        db = DatabaseManager(tmp_path)
        await db.initialize()
        try:
            items = await db.get_player_items("g1", "u1")
            assert {i["item_name"] for i in items} == {"铁剑", "盾牌"}
            assert not await db._table_exists("player_items_new")
        finally:
            await db.close()

    async def test_recovery_handles_missing_main_table(self, tmp_path):
        """直接验证「player_items 缺失」这条恢复分支（经由 initialize 时不会出现该状态）。"""
        import aiosqlite
        import sqlite3

        db_path = tmp_path / "ladder.db"
        self._make_old_schema_db(db_path)
        con = sqlite3.connect(db_path)
        con.executescript("""
            CREATE TABLE player_items_new (
                group_id TEXT NOT NULL, player_id TEXT NOT NULL, item_name TEXT NOT NULL,
                grade TEXT NOT NULL DEFAULT '', quantity INTEGER DEFAULT 1,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (group_id, player_id, item_name, grade)
            );
            INSERT INTO player_items_new (group_id, player_id, item_name, grade, quantity, updated_at)
                SELECT group_id, player_id, item_name,
                       CASE WHEN grade IS NULL THEN '' ELSE grade END, quantity, updated_at
                FROM player_items;
            DROP TABLE player_items;
        """)
        con.commit()
        con.close()

        db = DatabaseManager(tmp_path)
        db._db = await aiosqlite.connect(db.db_path)  # 绕过建表，直接进入恢复分支
        try:
            await db._recover_interrupted_grade_pk_migration()
            assert await db._table_exists("player_items")
            items = await db.get_player_items("g1", "u1")
            assert {i["item_name"] for i in items} == {"铁剑", "盾牌"}
        finally:
            await db.close()


class TestRegisterReply:
    """录入玩家的回复内容与具体信仰落库。"""

    @pytest.fixture
    async def service(self, db):
        return LadderService(db)

    async def test_reply_shows_path_and_specific_faith(self, service):
        """回复里应是「信仰：命途 | 具体信仰」，而不是旧的「命途: X」。"""
        ok, msg = await service.register_player(
            "g1", "张三", "生命", "战士", 1000, 100, "admin", specific_faith="繁荣"
        )
        assert ok is True
        assert "信仰：生命 | 繁荣" in msg
        assert "命途" not in msg

    async def test_specific_faith_is_persisted(self, service):
        """名片里解析到的具体信仰要落库，不能只出现在回复里。"""
        await service.register_player(
            "g1", "张三", "生命", "战士", 1000, 100, "admin", specific_faith="繁荣"
        )
        player = await service.db.get_player_by_name("g1", "张三")
        assert player.specific_faith == "繁荣"
        assert player.faith == "生命"

    async def test_reply_without_specific_faith(self, service):
        """只给命途时（非 @ 录入路径）不应出现多余的竖线。"""
        ok, msg = await service.register_player("g1", "李四", "虚无", "法师", 1000, 100, "admin")
        assert ok is True
        assert "信仰：虚无" in msg
        assert "信仰：虚无 |" not in msg

    async def test_per_faith_flavor_text_is_used(self, service):
        """信仰文案按具体信仰抽取。

        FAITH_MESSAGES 的键是 16 个具体信仰，此前用命途去查永远查不到，
        「录入玩家仪式化」实际一直退回通用文案。
        """
        from astrbot_plugin_faith_ladder.faith_messages import FAITH_MESSAGES
        pool = FAITH_MESSAGES["繁荣"]["register_success"]
        for _ in range(8):  # 文案是随机抽的，多跑几次确保落在该信仰的池子里
            ok, msg = await service.register_player(
                "g1", f"测试{_}", "生命", "战士", 1000, 100, "admin", specific_faith="繁荣"
            )
            assert ok is True
            assert any(line in msg for line in pool)

    async def test_no_cancel_promise_in_reply(self, service):
        """回复不应再承诺"否则将取消录入"（该机制早已移除）。"""
        ok, msg = await service.register_player(
            "g1", "张三", "生命", "战士", 1000, 100, "admin", specific_faith="繁荣"
        )
        assert ok is True
        assert "取消录入" not in msg


class TestFaithLineFormatter:
    """信仰行的统一渲染。"""

    def test_with_specific(self):
        from astrbot_plugin_faith_ladder.message_formatter import format_faith_line
        assert format_faith_line("生命", "繁荣") == "信仰：生命 | 繁荣"

    def test_path_only(self):
        from astrbot_plugin_faith_ladder.message_formatter import format_faith_line
        assert format_faith_line("虚无", None) == "信仰：虚无"

    def test_neither(self):
        from astrbot_plugin_faith_ladder.message_formatter import format_faith_line
        assert format_faith_line(None, None) == "信仰：未设定"


class TestUpsertAndBindingConcurrency:
    """并发注册/绑定不应把 IntegrityError 抛给调用方。"""

    async def test_concurrent_set_player_qq_returns_bool(self, db):
        await db.upsert_player("g1", "u1", "Alice")
        await db.upsert_player("g1", "u2", "Bob")

        results = await asyncio.gather(
            db.set_player_qq("g1", "u1", "123456"),
            db.set_player_qq("g1", "u2", "123456"),
        )
        assert sorted(results) == [False, True]
        # 同一个 QQ 只能绑到一个玩家
        assert (await db.get_player_by_qq("g1", "123456")) is not None

    async def test_set_player_qq_false_for_missing_player(self, db):
        """UPDATE 未命中任何行时应返回 False，而不是谎报绑定成功。"""
        assert await db.set_player_qq("g1", "nobody", "123456") is False
