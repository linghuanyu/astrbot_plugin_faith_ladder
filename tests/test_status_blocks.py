"""
状态阻断（block_actions）：给状态挂上"禁止做某事"，由入口闸门生效。

这是状态系统第一个真实游戏效果——在此之前状态只是展示。
"""

import pytest

from astrbot_plugin_faith_ladder.commands.config import ConfigMixin
from astrbot_plugin_faith_ladder.commands.gate import (
    ACTION_LABELS,
    GateMixin,
    format_block_actions,
    parse_block_actions,
)


class TestParseBlockActions:
    def test_labels_to_action_ids(self):
        assert parse_block_actions("祷词,赠送") == ("prayer,gift_send", [])

    def test_action_ids_are_accepted(self):
        assert parse_block_actions("prayer") == ("prayer", [])

    def test_fullwidth_comma(self):
        assert parse_block_actions("祷词，查询") == ("prayer,query", [])

    def test_duplicates_are_removed(self):
        assert parse_block_actions("祷词,祷词,赠送") == ("prayer,gift_send", [])

    def test_none_means_clear(self):
        assert parse_block_actions("无") == ("", [])
        assert parse_block_actions("") == ("", [])
        # 用户没写这一段：存储值为 None（表示"不动原有阻断项"），不算错误
        assert parse_block_actions(None) == (None, [])

    def test_unknown_items_are_reported(self):
        stored, unknown = parse_block_actions("祷词,飞升")
        assert stored is None
        assert unknown == ["飞升"]

    def test_format_round_trip(self):
        assert format_block_actions("prayer,gift_send") == "祷词、赠送"
        assert format_block_actions("") == ""
        assert format_block_actions(None) == ""


class _StatusHost(ConfigMixin, GateMixin):
    """闸门 + 一个固定的"发起人"。"""

    def __init__(self, db_manager, player, config=None):
        self.db_manager = db_manager
        self._player = player
        self.config = config or {}

    def _get_group_id(self, event):
        return "g1"

    async def _resolve_self_player_lenient(self, event):
        return self._player


class _Event:
    def get_sender_id(self):
        return "999"


class TestStatusBlockingByGate:
    async def _add(self, db_manager, player, status, days, block_actions):
        await db_manager.add_status("g1", player.player_id, status, days, block_actions)
        await db_manager.commit()

    async def test_status_blocks_its_action(self, db_manager):
        await db_manager.upsert_player("g1", "u1", "张三")
        player = await db_manager.get_player_by_name("g1", "张三")
        await self._add(db_manager, player, "沉默", 2, "prayer")

        host = _StatusHost(db_manager, player)
        blocked, msg = await host._gate(_Event(), "prayer")
        assert blocked is True
        assert "沉默" in msg and "祷词" in msg

    async def test_status_does_not_block_other_actions(self, db_manager):
        await db_manager.upsert_player("g1", "u1", "张三")
        player = await db_manager.get_player_by_name("g1", "张三")
        await self._add(db_manager, player, "沉默", 2, "prayer")

        host = _StatusHost(db_manager, player)
        assert await host._gate(_Event(), "gift_send") == (False, None)

    async def test_no_block_actions_means_no_block(self, db_manager):
        await db_manager.upsert_player("g1", "u1", "张三")
        player = await db_manager.get_player_by_name("g1", "张三")
        await self._add(db_manager, player, "虚弱", 2, None)

        host = _StatusHost(db_manager, player)
        assert await host._gate(_Event(), "prayer") == (False, None)

    async def test_expired_status_stops_blocking(self, db_manager):
        await db_manager.upsert_player("g1", "u1", "张三")
        player = await db_manager.get_player_by_name("g1", "张三")
        # 直接写入一条已过期的记录
        await db_manager._db.execute(
            "INSERT INTO player_statuses (group_id, player_id, status_name, expire_at, block_actions) "
            "VALUES (?, ?, ?, ?, ?)",
            ("g1", player.player_id, "沉默", "2000-01-01 00:00:00", "prayer"),
        )
        await db_manager.commit()

        host = _StatusHost(db_manager, player)
        assert await host._gate(_Event(), "prayer") == (False, None)

    async def test_unknown_actor_fails_open(self, db_manager):
        """身份识别不到时放行：用弱身份拦人反而会成为新的骚扰手段。"""
        host = _StatusHost(db_manager, None)
        assert await host._gate(_Event(), "prayer") == (False, None)

    async def test_non_blockable_action_skips_db(self, db_manager):
        """不在可阻断表里的动作（如排行榜）不查数据库。"""
        host = _StatusHost(db_manager, None)
        assert "scoreboard" not in ACTION_LABELS
        assert await host._gate(_Event(), "scoreboard") == (False, None)


class TestStatusStorageSemantics:
    async def test_add_without_block_keeps_existing(self, db_manager):
        """「添加状态 张三 沉默 3」只续期，不该顺带清掉原有的阻断项。"""
        await db_manager.upsert_player("g1", "u1", "张三")
        await db_manager.add_status("g1", "u1", "沉默", 3, "prayer")
        await db_manager.commit()

        await db_manager.add_status("g1", "u1", "沉默", 5)
        await db_manager.commit()

        statuses = await db_manager.get_player_statuses("g1", "u1")
        assert statuses[0]["block_actions"] == "prayer"
        assert statuses[0]["remaining_days"] >= 4

    async def test_empty_string_clears_block(self, db_manager):
        await db_manager.upsert_player("g1", "u1", "张三")
        await db_manager.add_status("g1", "u1", "沉默", 3, "prayer")
        await db_manager.commit()

        await db_manager.add_status("g1", "u1", "沉默", 3, "")
        await db_manager.commit()

        statuses = await db_manager.get_player_statuses("g1", "u1")
        assert statuses[0]["block_actions"] == ""

    async def test_batch_query_returns_block_actions(self, db_manager):
        await db_manager.upsert_player("g1", "u1", "张三")
        await db_manager.add_status("g1", "u1", "沉默", 3, "prayer,gift_send")
        await db_manager.commit()

        result = await db_manager.get_statuses_for_players("g1", ["u1"])
        assert result[0][1][0]["block_actions"] == "prayer,gift_send"


class TestBlockActionsMigration:
    async def test_legacy_table_gets_column(self, tmp_path):
        """老库（player_statuses 没有 block_actions 列）升级后自动补列。"""
        from astrbot_plugin_faith_ladder.db_manager import DatabaseManager

        db = DatabaseManager(tmp_path)
        await db.initialize()

        # 模拟老库：重建一张没有这两列的表。source 是 block_actions 之后才加的，
        # 所以真实的那个年代的老库两列都缺；而 get_player_statuses 现在也要读
        # source，升级路径必须两列都补上才能读写正常
        await db._db.execute("DROP TABLE player_statuses")
        await db._db.execute(
            "CREATE TABLE player_statuses ("
            "group_id TEXT NOT NULL, player_id TEXT NOT NULL, status_name TEXT NOT NULL, "
            "expire_at TIMESTAMP NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
            "PRIMARY KEY (group_id, player_id, status_name))"
        )
        await db._db.commit()

        # 与 initialize() 里的迁移顺序一致
        await db._migrate_status_block_actions()
        await db._migrate_status_source()

        async with db._db.execute("PRAGMA table_info(player_statuses)") as cursor:
            columns = [row[1] for row in await cursor.fetchall()]
        assert "block_actions" in columns

        # 迁移后新写入/读取都要正常工作
        await db.upsert_player("g1", "u1", "张三")
        await db.add_status("g1", "u1", "沉默", 1, "prayer")
        await db.commit()
        statuses = await db.get_player_statuses("g1", "u1")
        assert statuses[0]["block_actions"] == "prayer"
        await db.close()

    async def test_migration_is_idempotent(self, db_manager):
        await db_manager._migrate_status_block_actions()
        await db_manager._migrate_status_block_actions()
        async with db_manager._db.execute("PRAGMA table_info(player_statuses)") as cursor:
            columns = [row[1] for row in await cursor.fetchall()]
        assert columns.count("block_actions") == 1


class TestCardShowsBlockedActions:
    def test_card_lists_blocked_actions(self):
        from astrbot_plugin_faith_ladder.message_formatter import format_player_card
        from astrbot_plugin_faith_ladder.models import Player

        player = Player(player_id="u1", group_id="g1", player_name="张三")
        statuses = [
            {"status_name": "沉默", "expire_at": "2026-01-01 00:00:00", "remaining_days": 2,
             "block_actions": "prayer,gift_send"},
        ]
        text = format_player_card(player, 1, 1, 1000, 100, statuses)
        assert "沉默: 剩余2天（禁止：祷词、赠送）" in text

    def test_card_without_blocks_has_no_extra_text(self):
        from astrbot_plugin_faith_ladder.message_formatter import format_player_card
        from astrbot_plugin_faith_ladder.models import Player

        player = Player(player_id="u1", group_id="g1", player_name="张三")
        statuses = [
            {"status_name": "虚弱", "expire_at": "2026-01-01 00:00:00", "remaining_days": 1,
             "block_actions": None},
        ]
        text = format_player_card(player, 1, 1, 1000, 100, statuses)
        assert "禁止" not in text


class TestAddStatusCommandParsing:
    """指令层解析「阻断=…」：既要不影响原有三段式，也要能报错。"""

    class _Service:
        def __init__(self):
            self.calls = []

        async def add_status(self, group_id, player_name, status_name, days, block_actions=None):
            self.calls.append((group_id, player_name, status_name, days, block_actions))
            return True, "ok"

    class _Event:
        def __init__(self, text):
            self._text = text

        def get_sender_id(self):
            return "999"

        def plain_result(self, text):
            return text

    class _Host(ConfigMixin, GateMixin):
        def __init__(self, text):
            self.config = {}
            self.ladder_service = TestAddStatusCommandParsing._Service()
            self._text = text

        async def _check_perm(self, event):
            return True

        def _get_group_id(self, event):
            return "g1"

        def _get_args(self, event, cmd):
            return self._text

    async def _run(self, text):
        from astrbot_plugin_faith_ladder.commands.inventory import InventoryCommandsMixin

        host = self._Host(text)
        replies = [r async for r in InventoryCommandsMixin._add_status_impl(host, self._Event(text))]
        return host, replies

    async def test_without_block_segment_passes_none(self):
        host, _ = await self._run("张三 沉默 3")
        assert host.ladder_service.calls == [("g1", "张三", "沉默", 3, None)]

    async def test_with_block_segment_passes_ids(self):
        host, _ = await self._run("张三 沉默 3 阻断=祷词,赠送")
        assert host.ladder_service.calls == [("g1", "张三", "沉默", 3, "prayer,gift_send")]

    async def test_clear_segment_passes_empty_string(self):
        host, _ = await self._run("张三 沉默 3 阻断=无")
        assert host.ladder_service.calls == [("g1", "张三", "沉默", 3, "")]

    async def test_multi_word_status_name_still_works(self):
        host, _ = await self._run("张三 重度 虚弱 3 阻断=查询")
        assert host.ladder_service.calls == [("g1", "张三", "重度 虚弱", 3, "query")]

    async def test_unknown_block_item_reports_and_does_not_call_service(self):
        host, replies = await self._run("张三 沉默 3 阻断=飞升")
        assert host.ladder_service.calls == []
        assert any("无法识别的阻断项" in r for r in replies)
