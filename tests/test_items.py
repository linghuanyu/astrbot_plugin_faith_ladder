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

    @pytest.mark.asyncio
    async def test_delete_player_cleans_items(self, db):
        """Deleting a player should also delete their items."""
        await db.upsert_player("g1", "u1", "Alice")
        await db.add_item("g1", "u1", "铁剑", 2)
        await db.add_item("g1", "u1", "生命药水", 5)

        await db.delete_player("g1", "u1")

        items = await db.get_player_items("g1", "u1")
        assert len(items) == 0

    @pytest.mark.asyncio
    async def test_delete_all_players_cleans_items(self, db):
        """Deleting all players should also delete all items in the group."""
        await db.upsert_player("g1", "u1", "Alice")
        await db.upsert_player("g1", "u2", "Bob")
        await db.add_item("g1", "u1", "铁剑", 2)
        await db.add_item("g1", "u2", "生命药水", 5)

        await db.delete_all_players("g1")

        items_alice = await db.get_player_items("g1", "u1")
        items_bob = await db.get_player_items("g1", "u2")
        assert len(items_alice) == 0
        assert len(items_bob) == 0


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

    def test_parse_item_with_quantity(self, service):
        """Test item name with *quantity suffix is parsed correctly."""
        text = (
            "【玩家：张三，表现评分：A】\n"
            "【获得道具：美味糖果（C级）*3】\n"
            "【登神之路+10】\n"
        )
        results, err = service.parse_batch_scores(text)
        assert err is None
        assert len(results) == 1
        assert results[0]["name"] == "张三"
        # *3 means 3 of the same item
        assert results[0]["items"] == ["美味糖果（C级）", "美味糖果（C级）", "美味糖果（C级）"]
        assert results[0]["ladder_delta"] == 10

    def test_parse_alternative_ladder_name(self, service):
        """Test '封神之路' (alternative name) and colon format."""
        text = (
            "【玩家: 阡陌 寂灭使徒 1075 108 表现评分:A】\n"
            "【获得道具:泯灭手枪(B)】\n"
            "【封神之路:+14】\n"
            "【觐见之梯:+2】\n"
            "【当前登神之路得分:1089】\n"
            "【当前觐见之梯得分:110】\n"
        )
        results, err = service.parse_batch_scores(text)
        assert err is None
        assert len(results) == 1
        assert results[0]["name"] == "阡陌"
        assert results[0]["ladder_delta"] == 14
        assert results[0]["pilgrimage_delta"] == 2
        assert results[0]["items"] == ["泯灭手枪(B)"]

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
            {"item_name": "铁剑", "grade": None, "quantity": 2},
            {"item_name": "生命药水", "grade": None, "quantity": 5},
        ]
        result = format_inventory("Alice", items)
        assert "=== 储物空间 ===" in result
        assert "玩家: Alice" in result
        assert "铁剑 * 2" in result
        assert "生命药水 * 5" in result

    def test_graded_item(self):
        items = [{"item_name": "共生噬刃", "grade": "C", "quantity": 1}]
        result = format_inventory("Alice", items)
        assert "共生噬刃（C级）" in result


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
        assert "铁剑 * 2" in msg
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
        assert "铁剑 * 5" in msg
        items = await service.db.get_player_items("g1", "u1")
        assert len(items) == 0


class TestParseItemFullName:
    """Tests for parse_item_full_name."""

    def test_with_grade_parentheses(self):
        from astrbot_plugin_faith_ladder.ladder_service import parse_item_full_name
        base, grade = parse_item_full_name("共生噬刃（C级）")
        assert base == "共生噬刃"
        assert grade == "C"

    def test_with_grade_ascii_parentheses(self):
        from astrbot_plugin_faith_ladder.ladder_service import parse_item_full_name
        base, grade = parse_item_full_name("泯灭手枪(B)")
        assert base == "泯灭手枪"
        assert grade == "B"

    def test_without_grade(self):
        from astrbot_plugin_faith_ladder.ladder_service import parse_item_full_name
        base, grade = parse_item_full_name("铁剑")
        assert base == "铁剑"
        assert grade is None

    def test_uppercase_grade(self):
        from astrbot_plugin_faith_ladder.ladder_service import parse_item_full_name
        base, grade = parse_item_full_name("共生噬刃（sss级）")
        assert base == "共生噬刃"
        assert grade == "SSS"

    def test_invalid_grade(self):
        from astrbot_plugin_faith_ladder.ladder_service import parse_item_full_name
        base, grade = parse_item_full_name("铁剑（X级）")
        assert base == "铁剑（X级）"
        assert grade is None


class TestFormatItemDisplay:
    """Tests for format_item_display."""

    def test_with_grade_qty_gt_1(self):
        from astrbot_plugin_faith_ladder.ladder_service import format_item_display
        result = format_item_display("共生噬刃", "C", 3)
        assert result == "共生噬刃（C级） * 3"

    def test_with_grade_qty_1(self):
        from astrbot_plugin_faith_ladder.ladder_service import format_item_display
        result = format_item_display("共生噬刃", "C", 1)
        assert result == "共生噬刃（C级）"

    def test_no_grade_qty_gt_1(self):
        from astrbot_plugin_faith_ladder.ladder_service import format_item_display
        result = format_item_display("铁剑", None, 5)
        assert result == "铁剑 * 5"

    def test_no_grade_qty_1(self):
        from astrbot_plugin_faith_ladder.ladder_service import format_item_display
        result = format_item_display("铁剑", None, 1)
        assert result == "铁剑"


class TestClearItems:
    """Tests for clear_items."""

    @pytest.fixture
    async def service(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = DatabaseManager(Path(tmpdir))
            await db.initialize()
            svc = LadderService(db)
            yield svc
            await db.close()

    @pytest.mark.asyncio
    async def test_clear_all(self, service):
        await service.db.upsert_player("g1", "u1", "Alice")
        await service.db.add_item("g1", "u1", "铁剑", 3)
        await service.db.add_item("g1", "u1", "生命药水", 5)
        success, msg = await service.clear_items("g1", "Alice")
        assert success is True
        items = await service.db.get_player_items("g1", "u1")
        assert len(items) == 0

    @pytest.mark.asyncio
    async def test_clear_specific_item(self, service):
        await service.db.upsert_player("g1", "u1", "Alice")
        await service.db.add_item("g1", "u1", "铁剑", 3)
        await service.db.add_item("g1", "u1", "生命药水", 5)
        success, msg = await service.clear_items("g1", "Alice", "铁剑")
        assert success is True
        items = await service.db.get_player_items("g1", "u1")
        assert len(items) == 1
        assert items[0]["item_name"] == "生命药水"

    @pytest.mark.asyncio
    async def test_clear_nonexistent_player(self, service):
        success, msg = await service.clear_items("g1", "Ghost")
        assert success is False
        assert "不存在" in msg


class TestItemsWithGrade:
    """Tests for grade-separated item operations."""

    @pytest.fixture
    async def service(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = DatabaseManager(Path(tmpdir))
            await db.initialize()
            svc = LadderService(db)
            yield svc
            await db.close()

    @pytest.mark.asyncio
    async def test_give_item_with_grade(self, service):
        await service.db.upsert_player("g1", "u1", "Alice")
        success, msg = await service.give_items("g1", "Alice", [("共生噬刃（C级）", 2)])
        assert success is True
        items = await service.db.get_player_items("g1", "u1")
        assert len(items) == 1
        assert items[0]["item_name"] == "共生噬刃"
        assert items[0]["grade"] == "C"
        assert items[0]["quantity"] == 2

    @pytest.mark.asyncio
    async def test_take_item_by_name_without_grade(self, service):
        """收回道具时不需要指定等级，按 item_name 匹配。"""
        await service.db.upsert_player("g1", "u1", "Alice")
        await service.db.add_item("g1", "u1", "共生噬刃", 3, grade="C")
        success, msg = await service.take_items("g1", "Alice", [("共生噬刃", 2)])
        assert success is True
        items = await service.db.get_player_items("g1", "u1")
        assert items[0]["quantity"] == 1

    @pytest.mark.asyncio
    async def test_inventory_sorted_by_grade(self, service):
        """查询储物空间按等级从高到低排序。"""
        await service.db.upsert_player("g1", "u1", "Alice")
        await service.db.add_item("g1", "u1", "铁剑", 1)
        await service.db.add_item("g1", "u1", "短刀", 1, grade="C")
        await service.db.add_item("g1", "u1", "长刀", 1, grade="SSS")
        await service.db.add_item("g1", "u1", "大剑", 1, grade="A")

        items = await service.db.get_player_items("g1", "u1")
        grades = [i["grade"] for i in items]
        assert grades == ["SSS", "A", "C", None]

    @pytest.mark.asyncio
    async def test_deduct_item_with_grade(self, service):
        await service.db.upsert_player("g1", "u1", "Alice")
        await service.db.add_item("g1", "u1", "共生噬刃", 5, grade="C")
        success, msg, base_name, grade = await service.deduct_item(
            "g1", "u1", "Alice", "共生噬刃（C级）", 2
        )
        assert success is True
        assert base_name == "共生噬刃"
        assert grade == "C"
        items = await service.db.get_player_items("g1", "u1")
        assert items[0]["quantity"] == 3
