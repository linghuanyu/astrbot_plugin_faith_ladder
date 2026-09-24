"""
`player_statuses.source` 列（状态来源）测试。

为什么需要这一列：祈愿试炼给成员挂的状态与诸神手工添加的状态在表里同名同形
（状态名就是队名），而「每名玩家最多保留 N 条队伍状态」「队伍名单一变就撤销
对应状态」都必须准确认出哪些行是队伍写的。靠名字或前缀识别都不可靠——诸神既能
给队伍改名，也能手写一个同名状态。

来源一旦确定就不该被续期改写：诸神给一条队伍状态续期，不该把它从祈愿组队手里
收走，反向同理。所以 `add_status` 的 `source` 只在插入新行时生效。

注意读取方式：`add_status` **不自行 commit**（调用方负责），所以断言来源要走
同一个连接（`get_player_statuses`）；用独立 sqlite 连接读会看不到未提交的行。
"""

import sqlite3

import pytest
import pytest_asyncio

from astrbot_plugin_faith_ladder.db_manager import (
    STATUS_SOURCE_WISH,
    DatabaseManager,
)

# 升级前的表结构（`block_actions` 是更早一次迁移加的，source 是本次加的）
OLD_STATUSES_SCHEMA = """
CREATE TABLE player_statuses (
    group_id TEXT NOT NULL,
    player_id TEXT NOT NULL,
    status_name TEXT NOT NULL,
    expire_at TIMESTAMP NOT NULL,
    block_actions TEXT DEFAULT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (group_id, player_id, status_name)
)
"""


def _columns(db_path):
    """独立连接读表结构（DDL 已提交，不受未提交事务影响）。"""
    conn = sqlite3.connect(str(db_path))
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(player_statuses)").fetchall()]
    finally:
        conn.close()


async def _source_of(db, name):
    """读某个状态的来源；不存在返回 None（与 source=NULL 同形，调用方自行区分）。"""
    for s in await db.get_player_statuses("g1", "u1"):
        if s["status_name"] == name:
            return s["source"]
    return None


@pytest_asyncio.fixture
async def db(temp_data_dir):
    dbm = DatabaseManager(temp_data_dir)
    await dbm.initialize()
    yield dbm
    await dbm.close()


@pytest.mark.asyncio
async def test_add_status_records_source(db):
    await db.upsert_player("g1", "u1", "Alice")
    await db.add_status("g1", "u1", "09月28日祈愿试炼", 3, source=STATUS_SOURCE_WISH)

    assert await _source_of(db, "09月28日祈愿试炼") == "wish"


@pytest.mark.asyncio
async def test_add_status_without_source_stays_null(db):
    """不传 source 的既有调用（诸神「添加状态」）来源必须留空。"""
    await db.upsert_player("g1", "u1", "Alice")
    await db.add_status("g1", "u1", "虚弱", 3)

    statuses = await db.get_player_statuses("g1", "u1")
    assert len(statuses) == 1
    assert statuses[0]["source"] is None


@pytest.mark.asyncio
async def test_upsert_does_not_overwrite_source(db):
    """续期不得改写来源，两个方向都要钉住。"""
    await db.upsert_player("g1", "u1", "Alice")

    # 诸神先加，祈愿带着 source 续期 → 仍是诸神的
    await db.add_status("g1", "u1", "同名", 3)
    await db.add_status("g1", "u1", "同名", 5, source=STATUS_SOURCE_WISH)
    assert await _source_of(db, "同名") is None

    # 祈愿先挂，诸神续期 → 仍是祈愿的
    await db.add_status("g1", "u1", "队伍名", 3, source=STATUS_SOURCE_WISH)
    await db.add_status("g1", "u1", "队伍名", 5)
    assert await _source_of(db, "队伍名") == "wish"


@pytest.mark.asyncio
async def test_upsert_still_refreshes_expiry(db):
    """只加 source 参数，续期语义不能被顺手改掉。"""
    await db.upsert_player("g1", "u1", "Alice")
    await db.add_status("g1", "u1", "队伍名", 3, source=STATUS_SOURCE_WISH)
    await db.add_status("g1", "u1", "队伍名", 9)

    statuses = await db.get_player_statuses("g1", "u1")
    assert [s["remaining_days"] for s in statuses] == [9]


@pytest.mark.asyncio
async def test_block_actions_semantics_unchanged(db):
    """重构 add_status 为动态 SQL 时，block_actions 的三种行为必须原样保留。"""
    await db.upsert_player("g1", "u1", "Alice")

    await db.add_status("g1", "u1", "禁令", 3, block_actions="prayer")
    assert (await db.get_player_statuses("g1", "u1"))[0]["block_actions"] == "prayer"

    # None = 保持原有阻断项不变
    await db.add_status("g1", "u1", "禁令", 5)
    assert (await db.get_player_statuses("g1", "u1"))[0]["block_actions"] == "prayer"

    # 空串 = 清除
    await db.add_status("g1", "u1", "禁令", 5, block_actions="")
    assert (await db.get_player_statuses("g1", "u1"))[0]["block_actions"] == ""


@pytest.mark.asyncio
async def test_old_db_gains_source_column_with_null_rows(temp_data_dir):
    """老库升级：加列成功、旧行来源为空、状态照常可读（查询行为不变）。"""
    db_path = temp_data_dir / "ladder.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(OLD_STATUSES_SCHEMA)
    conn.execute(
        "INSERT INTO player_statuses (group_id, player_id, status_name, expire_at, block_actions) "
        "VALUES ('g1', 'u1', '旧状态', datetime('now', '+3 days'), 'prayer')"
    )
    conn.commit()
    conn.close()

    assert "source" not in _columns(db_path), "前提不成立：老库本就该没有 source 列"

    db = DatabaseManager(temp_data_dir)
    await db.initialize()
    try:
        assert "source" in _columns(db_path), "迁移没有给老库加上 source 列"

        statuses = await db.get_player_statuses("g1", "u1")
        assert len(statuses) == 1
        assert statuses[0]["status_name"] == "旧状态"
        assert statuses[0]["block_actions"] == "prayer"
        assert statuses[0]["source"] is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_second_initialize_is_idempotent(temp_data_dir):
    """重复 initialize（插件重载）不得因为列已存在而报错。"""
    db = DatabaseManager(temp_data_dir)
    await db.initialize()
    await db.close()

    db2 = DatabaseManager(temp_data_dir)
    await db2.initialize()
    try:
        await db2.upsert_player("g1", "u1", "Alice")
        await db2.add_status("g1", "u1", "队伍名", 3, source=STATUS_SOURCE_WISH)
        assert (await db2.get_player_statuses("g1", "u1"))[0]["source"] == "wish"
    finally:
        await db2.close()


@pytest.mark.asyncio
async def test_batch_query_also_returns_source(db):
    """批量查询（榜单/批量查档走的那条）也要带来源，否则祈愿那边只能写裸 SQL。"""
    await db.upsert_player("g1", "u1", "Alice")
    await db.add_status("g1", "u1", "队伍名", 3, source=STATUS_SOURCE_WISH)

    pairs = await db.get_statuses_for_players("g1", ["u1"])
    assert len(pairs) == 1
    assert pairs[0][1][0]["source"] == "wish"
