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

    async def test_remove_item_all_grades(self, db):
        """「全部收回」（数量为 None）仍删除该名字下的所有等级。"""
        await db.upsert_player("g1", "u1", "Alice")
        await db.add_item("g1", "u1", "铁剑", 5, grade=None)
        await db.add_item("g1", "u1", "铁剑", 5, grade="A")
        await db.commit()

        assert await db.remove_item("g1", "u1", "铁剑") is True
        assert await db.get_player_items("g1", "u1") == []


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
