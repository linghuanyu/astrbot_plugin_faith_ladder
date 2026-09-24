"""
一次性数据迁移的记账机制（`schema_migrations` 表）测试。

背景：`_migrate_item_names` 与 `migrate_player_items` 每次启动都全表读
`player_items` 并逐行查一次，数据库长大后启动成本随道具行数线性增长。
现在两者成功提交后落一条记账，之后的启动直接跳过。

记账的代价必须一并钉死：迁移**只跑一次**，不会去清理"记账之后才出现的脏数据"；
所以才需要 `migrate_player_items(force=True)`（「天梯榜管理 迁移储物空间」）兜底。
"""

import sqlite3

import pytest
import pytest_asyncio

from astrbot_plugin_faith_ladder.db_manager import (
    MIGRATION_ITEM_GRADE,
    MIGRATION_ITEM_NAMES,
    DatabaseManager,
)


def _raw(db_path, sql, params=()):
    """用独立连接读写（DatabaseManager 的连接可能已经关掉或正持有）。"""
    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.execute(sql, params)
        rows = cursor.fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def _markers(db_path):
    return {r[0] for r in _raw(db_path, "SELECT name FROM schema_migrations")}


@pytest.mark.asyncio
async def test_first_start_records_both_migrations(temp_data_dir):
    """首次启动把两条数据迁移都记账，之后不必再全表扫。"""
    db = DatabaseManager(temp_data_dir)
    await db.initialize()
    await db.close()

    assert _markers(temp_data_dir / "ladder.db") == {
        MIGRATION_ITEM_NAMES, MIGRATION_ITEM_GRADE,
    }


@pytest.mark.asyncio
async def test_marked_migration_is_skipped_on_later_start(temp_data_dir):
    """已记账后不再扫描：记账之后才出现的旧格式数据会留在原处，这正是需要 force 的原因。

    （数据必须选这条迁移真正会改写的形态——名字里带等级括号。'糖果*3' 这类
    `*N` 后缀归 `_migrate_item_names` 管，用它做断言等于什么都没测。）
    """
    db_path = temp_data_dir / "ladder.db"
    db = DatabaseManager(temp_data_dir)
    await db.initialize()
    await db.close()

    _raw(
        db_path,
        "INSERT INTO player_items (group_id, player_id, item_name, grade, quantity) "
        "VALUES ('g1', 'u1', '共生噬刃（C级）', '', 2)",
    )

    db2 = DatabaseManager(temp_data_dir)
    await db2.initialize()
    await db2.close()

    rows = _raw(db_path, "SELECT item_name, grade, quantity FROM player_items")
    assert rows == [("共生噬刃（C级）", "", 2)], "记账已存在却仍在改写 player_items"


@pytest.mark.asyncio
async def test_failed_migration_is_not_marked_and_next_start_retries(temp_data_dir, monkeypatch):
    """迁移失败不记账：下次启动必须重试，不能留下"以为做过"的库。"""
    from astrbot_plugin_faith_ladder import item_utils

    db_path = temp_data_dir / "ladder.db"
    db = DatabaseManager(temp_data_dir)
    await db.initialize()
    await db.close()

    # 造出"升级前的老库"：没有记账，且表里有一条旧格式数据（否则循环不执行，
    # 注入的失败根本不会触发）
    _raw(db_path, "DELETE FROM schema_migrations")
    _raw(
        db_path,
        "INSERT INTO player_items (group_id, player_id, item_name, grade, quantity) "
        "VALUES ('g1', 'u1', '共生噬刃（C级）', '', 2)",
    )

    def boom(name):
        raise RuntimeError("模拟迁移中途失败")

    monkeypatch.setattr(item_utils, "parse_item_full_name", boom)

    db2 = DatabaseManager(temp_data_dir)
    await db2.initialize()
    await db2.close()
    assert MIGRATION_ITEM_GRADE not in _markers(db_path)
    # 上一条（道具名清理）本身成功，应当照常记账——两条迁移互不牵连
    assert MIGRATION_ITEM_NAMES in _markers(db_path)
    assert [r[0] for r in _raw(db_path, "SELECT item_name FROM player_items")] == ["共生噬刃（C级）"]

    monkeypatch.undo()
    db3 = DatabaseManager(temp_data_dir)
    await db3.initialize()
    await db3.close()
    assert MIGRATION_ITEM_GRADE in _markers(db_path), "失败的那条迁移没有在下次启动重试"
    row = _raw(db_path, "SELECT item_name, grade, quantity FROM player_items")[0]
    assert row == ("共生噬刃", "C", 2)


@pytest.mark.asyncio
async def test_force_reruns_after_marking(temp_data_dir):
    """force=True 绕过记账，供「天梯榜管理 迁移储物空间」手动重跑。"""
    db = DatabaseManager(temp_data_dir)
    await db.initialize()

    # 模拟"记账之后又出现的旧格式数据"
    await db.add_item("g1", "u1", "共生噬刃（C级）", 2)

    assert await db.migrate_player_items() == 0
    items = await db.get_player_items("g1", "u1")
    assert items[0]["item_name"] == "共生噬刃（C级）"

    assert await db.migrate_player_items(force=True) == 1
    items = await db.get_player_items("g1", "u1")
    assert (items[0]["item_name"], items[0]["grade"], items[0]["quantity"]) == ("共生噬刃", "C", 2)

    await db.close()
