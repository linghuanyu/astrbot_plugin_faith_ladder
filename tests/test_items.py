"""
Tests for player inventory (储物空间) system.
"""

import pytest
import tempfile
from pathlib import Path

from astrbot_plugin_faith_ladder.db_manager import DatabaseManager
from astrbot_plugin_faith_ladder.ladder_service import LadderService
from astrbot_plugin_faith_ladder.message_formatter import format_inventory


class TestDatabaseItems:
    """Tests for DB item CRUD methods."""

    @pytest.fixture
    async def db(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dbm = DatabaseManager(Path(tmpdir))
            await dbm.initialize()
            yield dbm
            await dbm.close()

    @pytest.mark.asyncio
    async def test_add_item(self, db):
        await db.upsert_player("g1", "u1", "Alice")
        await db.add_item("g1", "u1", "铁剑", 2)
        items = await db.get_player_items("g1", "u1")
        assert len(items) == 1
        assert items[0]["item_name"] == "铁剑"
        assert items[0]["quantity"] == 2

    @pytest.mark.asyncio
    async def test_add_item_accumulates(self, db):
        await db.upsert_player("g1", "u1", "Alice")
        await db.add_item("g1", "u1", "铁剑", 2)
        await db.add_item("g1", "u1", "铁剑", 3)
        items = await db.get_player_items("g1", "u1")
        assert len(items) == 1
        assert items[0]["quantity"] == 5

    @pytest.mark.asyncio
    async def test_add_item_zero_ignored(self, db):
        await db.upsert_player("g1", "u1", "Alice")
        await db.add_item("g1", "u1", "铁剑", 0)
        items = await db.get_player_items("g1", "u1")
        assert len(items) == 0

    @pytest.mark.asyncio
    async def test_remove_item_partial(self, db):
        await db.upsert_player("g1", "u1", "Alice")
        await db.add_item("g1", "u1", "铁剑", 5)
        result = await db.remove_item("g1", "u1", "铁剑", 2)
        assert result is True
        items = await db.get_player_items("g1", "u1")
        assert items[0]["quantity"] == 3

    @pytest.mark.asyncio
    async def test_remove_item_all(self, db):
        await db.upsert_player("g1", "u1", "Alice")
        await db.add_item("g1", "u1", "铁剑", 5)
        result = await db.remove_item("g1", "u1", "铁剑")
        assert result is True
        items = await db.get_player_items("g1", "u1")
        assert len(items) == 0

    @pytest.mark.asyncio
    async def test_remove_item_excess_deletes(self, db):
        await db.upsert_player("g1", "u1", "Alice")
        await db.add_item("g1", "u1", "铁剑", 2)
        result = await db.remove_item("g1", "u1", "铁剑", 10)
        assert result is True
        items = await db.get_player_items("g1", "u1")
        assert len(items) == 0

    @pytest.mark.asyncio
    async def test_remove_nonexistent_item(self, db):
        await db.upsert_player("g1", "u1", "Alice")
        result = await db.remove_item("g1", "u1", "幽灵剑", 1)
        assert result is False

    @pytest.mark.asyncio
    async def test_item_with_grade(self, db):
        """Test item name with grade in parentheses."""
        await db.upsert_player("g1", "u1", "Alice")
        await db.add_item("g1", "u1", "共生噬刃（C级）", 1)
        items = await db.get_player_items("g1", "u1")
        assert len(items) == 1
        assert items[0]["item_name"] == "共生噬刃（C级）"

    @pytest.mark.asyncio
    async def test_delete_all_items(self, db):
        await db.upsert_player("g1", "u1", "Alice")
        await db.add_item("g1", "u1", "铁剑", 2)
        await db.add_item("g1", "u1", "生命药水", 5)
        count = await db.delete_all_items("g1", "u1")
        assert count == 2
        items = await db.get_player_items("g1", "u1")
        assert len(items) == 0


class TestBatchParseWithItems:
    """Tests for batch parsing with items."""

    @pytest.fixture
    async def service(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = DatabaseManager(Path(tmpdir))
            await db.initialize()
            svc = LadderService(db)
            yield svc
            await db.close()

    def test_parse_with_items(self, service):
        text = (
            "【玩家：张三 表现评分：A】\n"
            "【登神之路+16】\n"
            "【觐见之梯+3】\n"
            "【获得道具：铁剑】\n"
            "【获得道具：生命药水】\n"
        )
        results, err = service.parse_batch_scores(text)
        assert err is None
        assert len(results) == 1
        assert results[0]["name"] == "张三"
        assert results[0]["ladder_delta"] == 16
        assert results[0]["pilgrimage_delta"] == 3
        assert results[0]["items"] == ["铁剑", "生命药水"]

    def test_parse_no_item(self, service):
        """Test that '无' is filtered out."""
        text = (
            "【玩家：张三】\n"
            "【获得道具：无】\n"
            "【登神之路+5】\n"
        )
        results, err = service.parse_batch_scores(text)
        assert err is None
        assert len(results) == 1
        assert results[0]["items"] == []

    def test_parse_space_separated_items(self, service):
        """Test space-separated items on one line."""
        text = (
            "【玩家：繁荣，表现评分：B】\n"
            "【获得道具：望远镜（C） 生锈的钥匙（B）】\n"
            "【登神之路+13】\n"
            "【觐见之梯+3】\n"
        )
        results, err = service.parse_batch_scores(text)
        assert err is None
        assert len(results) == 1
        assert results[0]["name"] == "繁荣"
        assert results[0]["items"] == ["望远镜（C）", "生锈的钥匙（B）"]
        assert results[0]["ladder_delta"] == 13
        assert results[0]["pilgrimage_delta"] == 3

    def test_parse_with_graded_items(self, service):
        text = (
            "【玩家：半秒失忆 旧日追猎者 1030.107表现评分：A】\n"
            "【获得道具：共生噬刃（C级）】\n"
            "【登神之路+16】\n"
            "【觐见之梯+3】\n"
            "【当前登神之路得分：146】\n"
            "【当前觐见之梯得分：110】\n"
        )
        results, err = service.parse_batch_scores(text)
        assert err is None
        assert len(results) == 1
        assert results[0]["name"] == "半秒失忆"
        assert results[0]["items"] == ["共生噬刃（C级）"]

    def test_parse_items_only_no_scores(self, service):
        text = (
            "【玩家：李四】\n"
            "【获得道具：铁剑】\n"
            "【获得道具：铁剑】\n"
        )
        results, err = service.parse_batch_scores(text)
        assert err is None
        assert len(results) == 1
        assert results[0]["items"] == ["铁剑", "铁剑"]

    def test_parse_no_items_no_scores(self, service):
        text = "【玩家：王五 表现评分：B】\n"
        results, err = service.parse_batch_scores(text)
        assert err is not None  # no valid data

    def test_parse_real_format(self, service):
        """Test parsing the actual game result format with multiple items per line and '无'."""
        text = (
            "【特殊试炼【孤岛骗局（欺诈）】挑战？？】\n"
            "【正在评分，并结算奖励……】\n"
            "\n"
            "【玩家：陈墨，表现评分：D】\n"
            "【获得道具：无】\n"
            "【登神之路+0】\n"
            "【觐见之梯+1】\n"
            "\n"
            "【玩家：拥抱，表现评分：B】\n"
            "【获得道具：望远镜（C） 生锈的钥匙（B）】\n"
            "【登神之路+13】\n"
            "【觐见之梯+3】\n"
            "\n"
            "【玩家：温迪，表现评分：C】\n"
            "【获得道具：无】\n"
            "【登神之路+5】\n"
            "【觐见之梯+1】\n"
        )
        results, err = service.parse_batch_scores(text)
        assert err is None
        assert len(results) == 3

        # 陈墨: no items (无 is filtered out)
        assert results[0]["name"] == "陈墨"
        assert results[0]["items"] == []
        assert results[0]["ladder_delta"] == 0
        assert results[0]["pilgrimage_delta"] == 1

        # 拥抱: two items separated by space
        assert results[1]["name"] == "拥抱"
        assert results[1]["items"] == ["望远镜（C）", "生锈的钥匙（B）"]
        assert results[1]["ladder_delta"] == 13
        assert results[1]["pilgrimage_delta"] == 3

        # 温迪: no items (无 is filtered out)
        assert results[2]["name"] == "温迪"
        assert results[2]["items"] == []


class TestFormatInventory:
    """Tests for format_inventory."""

    def test_empty_inventory(self):
        result = format_inventory("Alice", [])
        assert "储物空间为空" in result
        assert "Alice" in result

    def test_with_items(self):
        items = [
            {"item_name": "铁剑", "quantity": 2},
            {"item_name": "生命药水", "quantity": 5},
        ]
        result = format_inventory("Alice", items)
        assert "=== 储物空间 ===" in result
        assert "玩家: Alice" in result
        assert "铁剑 * 2" in result
        assert "生命药水 * 5" in result

    def test_graded_item(self):
        items = [{"item_name": "共生噬刃（C级）", "quantity": 1}]
        result = format_inventory("Alice", items)
        assert "共生噬刃（C级） * 1" in result


class TestGiveAndTakeItems:
    """Tests for give_items and take_items."""

    @pytest.fixture
    async def service(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = DatabaseManager(Path(tmpdir))
            await db.initialize()
            svc = LadderService(db)
            yield svc
            await db.close()

    @pytest.mark.asyncio
    async def test_give_items(self, service):
        await service.db.upsert_player("g1", "u1", "Alice")
        success, msg = await service.give_items("g1", "Alice", [("铁剑", 2), ("生命药水", 3)])
        assert success is True
        assert "铁剑*2" in msg
        items = await service.db.get_player_items("g1", "u1")
        assert len(items) == 2

    @pytest.mark.asyncio
    async def test_give_items_nonexistent_player(self, service):
        success, msg = await service.give_items("g1", "Ghost", [("铁剑", 1)])
        assert success is False
        assert "不存在" in msg

    @pytest.mark.asyncio
    async def test_take_items_partial(self, service):
        await service.db.upsert_player("g1", "u1", "Alice")
        await service.db.add_item("g1", "u1", "铁剑", 5)
        success, msg = await service.take_items("g1", "Alice", [("铁剑", 2)])
        assert success is True
        items = await service.db.get_player_items("g1", "u1")
        assert items[0]["quantity"] == 3

    @pytest.mark.asyncio
    async def test_take_items_all(self, service):
        await service.db.upsert_player("g1", "u1", "Alice")
        await service.db.add_item("g1", "u1", "铁剑", 5)
        success, msg = await service.take_items("g1", "Alice", [("铁剑", None)])
        assert success is True
        assert "全部" in msg
        items = await service.db.get_player_items("g1", "u1")
        assert len(items) == 0
