"""
改状态名（`重命名状态` / `祈愿管理 重命名` 共用的能力）测试。

两条容易被写错的地方，这里都钉住：

1. **必须原地改名、不能"删了按新名重挂"**：`add_status` 是 `now + days`，
   用它重挂会把剩余期限洗掉（还剩 1 天的状态会变成 3 天）。改名要保住
   `expire_at` / `block_actions` / `source`。
2. **目标名已存在时要合并，且合并方向不能反**：到期时间取**更晚的**（不让改名
   缩短惩罚），阻断项与来源保留**目标行原有的**（阻断属于那条惩罚本身；来源决定
   这条状态归谁管，不能被改名改掉）。
"""

import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio

from astrbot_plugin_faith_ladder.db_manager import STATUS_SOURCE_WISH, DatabaseManager
from astrbot_plugin_faith_ladder.ladder_service import LadderService
from astrbot_plugin_faith_ladder.messages import PLAYER_NOT_FOUND
from astrbot_plugin_faith_ladder.text_utils import split_rename_pair


def _utc(days: float) -> str:
    """UTC 时间戳字符串，与 add_status 的存储格式一致。"""
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


async def _seed(db, name, *, days, block=None, source=None):
    """直接落一条状态，用于精确控制 expire_at 与元数据。"""
    await db._db.execute(
        "INSERT INTO player_statuses (group_id, player_id, status_name, expire_at, block_actions, source) "
        "VALUES ('g1', 'u1', ?, ?, ?, ?)",
        (name, _utc(days), block, source),
    )
    await db._db.commit()


async def _rows(db):
    async with db._db.execute(
        "SELECT status_name, expire_at, block_actions, source FROM player_statuses "
        "WHERE group_id='g1' AND player_id='u1' ORDER BY status_name"
    ) as cursor:
        return [tuple(r) for r in await cursor.fetchall()]


@pytest_asyncio.fixture
async def db(temp_data_dir):
    dbm = DatabaseManager(temp_data_dir)
    await dbm.initialize()
    yield dbm
    await dbm.close()


@pytest_asyncio.fixture
async def service():
    with tempfile.TemporaryDirectory() as tmpdir:
        dbm = DatabaseManager(Path(tmpdir))
        await dbm.initialize()
        yield LadderService(dbm)
        await dbm.close()


class TestDbRenameStatus:
    async def test_not_found_when_old_missing(self, db):
        assert await db.rename_status("g1", "u1", "不存在", "新名") == "not_found"

    async def test_in_place_keeps_expiry_block_and_source(self, db):
        await _seed(db, "旧名", days=2, block="prayer", source=STATUS_SOURCE_WISH)

        assert await db.rename_status("g1", "u1", "旧名", "新名") == "ok"
        await db.commit()

        rows = await _rows(db)
        assert len(rows) == 1
        name, expire_at, block, source = rows[0]
        assert name == "新名"
        assert block == "prayer"
        assert source == STATUS_SOURCE_WISH
        # 期限必须原样保留（不是 now+days 重算）
        expected = _utc(2)
        assert expire_at[:16] == expected[:16]

    async def test_same_name_is_noop(self, db):
        await _seed(db, "原名", days=2, block="prayer")
        before = await _rows(db)

        assert await db.rename_status("g1", "u1", "原名", "原名") == "ok"
        await db.commit()

        assert await _rows(db) == before

    async def test_merge_takes_later_expiry(self, db):
        """改名的目标已存在且更晚 → 取更晚的到期时间。"""
        await _seed(db, "旧名", days=1)
        await _seed(db, "新名", days=5)

        assert await db.rename_status("g1", "u1", "旧名", "新名") == "merged"
        await db.commit()

        rows = await _rows(db)
        assert len(rows) == 1, "合并后只该剩一行"
        assert rows[0][0] == "新名"
        assert rows[0][1][:16] == _utc(5)[:16], "合并把到期时间改短了"

    async def test_merge_never_shortens_even_when_target_is_sooner(self, db):
        """改名的来源更晚 → 仍取更晚的，绝不缩短。"""
        await _seed(db, "旧名", days=9)
        await _seed(db, "新名", days=1)

        assert await db.rename_status("g1", "u1", "旧名", "新名") == "merged"
        await db.commit()

        rows = await _rows(db)
        assert len(rows) == 1
        assert rows[0][1][:16] == _utc(9)[:16], "合并把更晚的那条弄丢了"

    async def test_merge_keeps_target_block_and_source(self, db):
        """阻断项与来源保留目标行原有的，不因改名而改变归属。"""
        await _seed(db, "旧名", days=9, block="prayer", source=STATUS_SOURCE_WISH)
        await _seed(db, "新名", days=1, block="", source=None)

        assert await db.rename_status("g1", "u1", "旧名", "新名") == "merged"
        await db.commit()

        name, expire_at, block, source = (await _rows(db))[0]
        assert block == "", "阻断项应保留目标行原有的"
        assert source is None, "来源应保留目标行原有的"
        assert expire_at[:16] == _utc(9)[:16]

    async def test_rename_does_not_touch_other_players(self, db):
        """只动指定玩家：同名状态在别人身上不该被牵连。"""
        await db.upsert_player("g1", "u2", "Bob")
        await _seed(db, "旧名", days=2)
        await db._db.execute(
            "INSERT INTO player_statuses (group_id, player_id, status_name, expire_at) "
            "VALUES ('g1', 'u2', '旧名', ?)",
            (_utc(2),),
        )
        await db._db.commit()

        assert await db.rename_status("g1", "u1", "旧名", "新名") == "ok"
        await db.commit()

        async with db._db.execute(
            "SELECT status_name FROM player_statuses WHERE group_id='g1' AND player_id='u2'"
        ) as cursor:
            assert [r[0] for r in await cursor.fetchall()] == ["旧名"]


class TestServiceRenameStatus:
    async def test_reports_success(self, service):
        await service.db.upsert_player("g1", "u1", "张三")
        await service.db.add_status("g1", "u1", "虚弱", 3)
        await service.db.commit()

        ok, msg = await service.rename_status("g1", "张三", "虚弱", "强健")
        assert ok is True
        assert "虚弱" in msg and "强健" in msg

        names = [s["status_name"] for s in await service.db.get_player_statuses("g1", "u1")]
        assert names == ["强健"]

    async def test_commits_so_later_reads_see_it(self, service, temp_data_dir):
        """老库读取验证：改名后换一条独立连接也必须看得到（说明真的提交了）。"""
        await service.db.upsert_player("g1", "u1", "张三")
        await service.db.add_status("g1", "u1", "虚弱", 3)
        await service.db.commit()

        await service.rename_status("g1", "张三", "虚弱", "强健")

        conn = sqlite3.connect(str(service.db.db_path))
        try:
            rows = conn.execute(
                "SELECT status_name FROM player_statuses WHERE group_id='g1' AND player_id='u1'"
            ).fetchall()
        finally:
            conn.close()
        assert [r[0] for r in rows] == ["强健"]

    async def test_unknown_player(self, service):
        ok, msg = await service.rename_status("g1", "Ghost", "虚弱", "强健")
        assert ok is False
        assert msg == PLAYER_NOT_FOUND.format(name="Ghost")

    async def test_missing_status(self, service):
        await service.db.upsert_player("g1", "u1", "张三")

        ok, msg = await service.rename_status("g1", "张三", "幽灵", "强健")
        assert ok is False
        assert "没有状态" in msg and "幽灵" in msg

    async def test_merge_message_says_merged(self, service):
        await service.db.upsert_player("g1", "u1", "张三")
        await service.db.add_status("g1", "u1", "旧名", 2)
        await service.db.add_status("g1", "u1", "新名", 6)
        await service.db.commit()

        ok, msg = await service.rename_status("g1", "张三", "旧名", "新名")
        assert ok is True
        assert "合并" in msg


class TestSplitStatusRename:
    """`重命名状态` 的参数解析：状态名可以含空格，所以优先认显式分隔符。"""

    def test_two_plain_tokens(self):
        assert split_rename_pair("虚弱 强健") == ("虚弱", "强健")

    def test_arrow_allows_spaces(self):
        assert split_rename_pair("旧 名 → 新 名") == ("旧 名", "新 名")

    def test_ascii_arrow(self):
        assert split_rename_pair("旧 名 -> 新 名") == ("旧 名", "新 名")

    def test_extra_tokens_without_separator_rejected(self):
        assert split_rename_pair("旧 状 态 名") is None

    def test_single_token_rejected(self):
        assert split_rename_pair("只有一个") is None

    def test_empty_side_rejected(self):
        assert split_rename_pair("旧名 →") is None
        assert split_rename_pair("→ 新名") is None
