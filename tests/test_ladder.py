"""
Tests for the ladder service business logic.
"""

import pytest
from astrbot_plugin_faith_ladder.ladder_service import LadderService


@pytest.mark.asyncio
class TestLadderService:
    """Tests for LadderService."""

    async def test_get_leaderboard_empty(self, db_manager):
        """Test leaderboard with no players."""
        service = LadderService(db_manager)
        text = await service.get_leaderboard_text("g1", 10)
        assert "暂无排名数据" in text

    async def test_get_leaderboard_with_players(self, db_manager):
        """Test leaderboard with players."""
        service = LadderService(db_manager)

        await db_manager.upsert_player("g1", "u1", "Alice")
        await db_manager.upsert_player("g1", "u2", "Bob")
        await db_manager.update_scores("g1", "u1", 100, 50, "admin")
        await db_manager.update_scores("g1", "u2", 200, 30, "admin")

        text = await service.get_leaderboard_text("g1", 10)
        # 位阶徽记会出现在名字前（如「1. ☽ Bob」），这里只校验名次顺序
        rank_lines = [line for line in text.splitlines() if line.startswith(("1.", "2."))]
        assert rank_lines[0].endswith("Bob")
        assert rank_lines[1].endswith("Alice")
        assert "登神之路" in text
        assert "觐见之梯" in text

    async def test_get_player_card_by_name(self, db_manager):
        """Test getting player card by name."""
        service = LadderService(db_manager)
        await db_manager.upsert_player("g1", "u1", "Alice")
        await db_manager.update_scores("g1", "u1", 100, 50, "admin")

        text = await service.get_player_card_by_name("g1", "Alice")
        assert text is not None
        assert "Alice" in text

    async def test_add_score_new_player(self, db_manager):
        """Test adding score to a non-existent player returns failure (no auto-create)."""
        service = LadderService(db_manager)
        success, msg = await service.add_score("g1", "u1", "Alice", 100, 50, "admin")
        assert success is False
        assert "在本宇宙未寻找到" in msg

    async def test_add_score_existing_player(self, db_manager):
        """Test adding score to an existing player."""
        service = LadderService(db_manager)
        await db_manager.upsert_player("g1", "u1", "Alice")
        await db_manager.update_scores("g1", "u1", 100, 50, "admin")

        success, msg = await service.add_score("g1", "u1", "Alice", 30, 20, "admin")
        assert success is True

        player = await db_manager.get_player("g1", "u1")
        assert player.ladder_score == 1130  # 1000 initial + 100 + 30
        assert player.pilgrimage_score == 170  # 100 initial + 50 + 20

    async def test_set_class_valid(self, db_manager):
        """Test setting valid class."""
        service = LadderService(db_manager)
        await db_manager.upsert_player("g1", "u1", "Alice")

        success, msg = await service.set_class("g1", "u1", "Alice", "法师")
        assert success is True
        assert "法师" in msg

    async def test_set_class_invalid_class(self, db_manager):
        """Test setting invalid class."""
        service = LadderService(db_manager)
        await db_manager.upsert_player("g1", "u1", "Alice")

        success, msg = await service.set_class("g1", "u1", "Alice", "无效职业")
        assert success is False
        assert "无效职业" in msg

    async def test_set_class_nonexistent_player(self, db_manager):
        """Test setting class on non-existent player returns failure (no auto-create)."""
        service = LadderService(db_manager)

        success, msg = await service.set_class("g1", "u1", "NewPlayer", "战士")
        assert success is False
        assert "在本宇宙未寻找到" in msg


class TestLeaderboardScoreThreshold:
    """天梯榜的分数门槛：低于门槛不上榜，且过滤必须发生在 LIMIT 之前。"""

    async def test_players_below_threshold_are_excluded(self, db_manager):
        service = LadderService(db_manager)
        await db_manager.upsert_player("g1", "u1", "Alice")            # 1000 分
        await db_manager.upsert_player("g1", "u2", "Bob")
        await db_manager.update_scores("g1", "u2", 150, 0, "admin")    # 1150 分

        text = await service.get_leaderboard_text("g1", 10, 1100)
        assert "Bob" in text
        assert "Alice" not in text, "低于门槛的玩家不应出现在榜上"

    async def test_threshold_does_not_waste_slots(self, db_manager):
        """低分玩家不能占掉显示名额：先 LIMIT 再在上层丢弃会让榜上人数不足。"""
        service = LadderService(db_manager)
        await db_manager.upsert_player("g1", "u1", "LowA")
        await db_manager.upsert_player("g1", "u2", "LowB")
        await db_manager.upsert_player("g1", "u3", "High")
        await db_manager.update_scores("g1", "u3", 300, 0, "admin")    # 1300 分

        text = await service.get_leaderboard_text("g1", 1, 1100)
        assert "High" in text
        assert "LowA" not in text

    async def test_empty_board_explains_threshold(self, db_manager):
        """全员低于门槛时说明原因，避免被当成数据丢失。"""
        service = LadderService(db_manager)
        await db_manager.upsert_player("g1", "u1", "Alice")

        text = await service.get_leaderboard_text("g1", 10, 1100)
        assert "暂无排名数据" in text
        assert "1100" in text

    async def test_cache_key_includes_threshold(self, db_manager):
        """门槛不同不得共用缓存（否则改完配置仍按旧门槛显示 30 秒）。"""
        service = LadderService(db_manager)
        await db_manager.upsert_player("g1", "u1", "Alice")

        assert "Alice" in await service.get_leaderboard_text("g1", 10, 0)
        assert "Alice" not in await service.get_leaderboard_text("g1", 10, 1100)


class TestLadderCommandThresholdWiring:
    """命令层必须把配置里的门槛传给服务层（默认 1100）。"""

    from astrbot_plugin_faith_ladder.commands.config import ConfigMixin as _ConfigMixin
    from astrbot_plugin_faith_ladder.commands.gate import GateMixin as _GateMixin

    class _Event:
        def __init__(self):
            self.stopped = False

        def get_sender_id(self):
            return "10001"

        def plain_result(self, text):
            return text

        def stop_event(self):
            self.stopped = True

    class _Service:
        def __init__(self):
            self.calls = []

        async def get_leaderboard_text(self, group_id, limit, min_ladder_score):
            self.calls.append((group_id, limit, min_ladder_score))
            return "榜单文本"

    class _Cooldown:
        def check_cooldown(self, key, seconds):
            return True

        def set_cooldown(self, key):
            pass

    class _Host(_ConfigMixin, _GateMixin):
        def __init__(self, service, config):
            self.ladder_service = service
            self.config = config
            self.cooldown_manager = TestLadderCommandThresholdWiring._Cooldown()

        async def _check_perm(self, event):
            return True

        def _get_group_id(self, event):
            return "g1"

        async def _send_forward_text(self, event, group_id, title, text):
            return True  # 已按合并转发发出，命令就此结束

    async def _run(self, config):
        from astrbot_plugin_faith_ladder.commands.scoreboard import ScoreboardCommandsMixin

        service = self._Service()
        host = self._Host(service, config)
        _ = [r async for r in ScoreboardCommandsMixin._ladder_impl(host, self._Event())]
        return service.calls

    async def test_default_threshold_is_1100(self):
        assert await self._run({}) == [("g1", 10, 1100)]

    async def test_threshold_and_limit_come_from_config(self):
        calls = await self._run({"leaderboard_min_ladder_score": 0, "ladder_display_limit": 5})
        assert calls == [("g1", 5, 0)]


class TestLeaderboardCacheTtl:
    """榜单缓存时长（leaderboard_cache_seconds）：默认 120 秒，0 = 不缓存。

    插件内任何改分/录入/改名/改信仰都会显式失效缓存，所以 TTL 只影响"没有写入时
    的兜底新鲜度"——这里既测命中与关闭，也测长 TTL 下写入后依然立刻可见。
    """

    async def test_default_ttl_is_120(self):
        from astrbot_plugin_faith_ladder.plugin_config import schema_default

        assert schema_default("leaderboard_cache_seconds") == 120

    async def test_cache_hit_avoids_second_query(self, db_manager):
        calls = {"n": 0}
        original = db_manager.get_top_players

        async def counting(*args, **kwargs):
            calls["n"] += 1
            return await original(*args, **kwargs)

        db_manager.get_top_players = counting
        service = LadderService(db_manager, ttl_getter=lambda: 120)
        await service.get_leaderboard_text("g1", 10, 0)
        await service.get_leaderboard_text("g1", 10, 0)
        assert calls["n"] == 1

    async def test_zero_ttl_disables_cache(self, db_manager):
        calls = {"n": 0}
        original = db_manager.get_top_players

        async def counting(*args, **kwargs):
            calls["n"] += 1
            return await original(*args, **kwargs)

        db_manager.get_top_players = counting
        service = LadderService(db_manager, ttl_getter=lambda: 0)
        await service.get_leaderboard_text("g1", 10, 0)
        await service.get_leaderboard_text("g1", 10, 0)
        assert calls["n"] == 2, "配置成 0 时每次都应查库"

    async def test_bad_ttl_value_falls_back_to_default(self, db_manager):
        service = LadderService(db_manager, ttl_getter=lambda: "abc")
        assert service._cache_ttl() == LadderService.LEADERBOARD_CACHE_TTL
        service = LadderService(db_manager, ttl_getter=lambda: -5)
        assert service._cache_ttl() == 0

    async def test_register_player_is_visible_immediately_with_long_ttl(self, db_manager):
        """录入玩家后必须立刻上榜：长 TTL 不能把新玩家藏起来。"""
        service = LadderService(db_manager, ttl_getter=lambda: 3600)
        assert "Alice" not in await service.get_leaderboard_text("g1", 10, 0)

        await service.register_player("g1", "Alice", "生命", "战士", 1200, 100, "admin")
        assert "Alice" in await service.get_leaderboard_text("g1", 10, 0)

    async def test_register_reply_carries_god_lore_line(self, db_manager):
        """录入回复追加一行神明定称号 / 谕行（原文）；原著没写的信仰不加。"""
        service = LadderService(db_manager)
        ok, msg = await service.register_player(
            "g1", "Alice", "文明", "牧师", 1000, 100, "admin", specific_faith="秩序"
        )
        assert ok is True
        assert "文明的序幕" in msg

    async def test_register_reply_skips_lore_for_faiths_without_source(self, db_manager):
        """沉默在原著里没有可引用的定语/谕行——宁可不发，也不杜撰。"""
        service = LadderService(db_manager)
        ok, msg = await service.register_player(
            "g1", "Bob", "混沌", "战士", 1000, 100, "admin", specific_faith="沉默"
        )
        assert ok is True
        assert "命途的" not in msg

    async def test_class_change_is_visible_immediately_with_long_ttl(self, db_manager):
        service = LadderService(db_manager, ttl_getter=lambda: 3600)
        await db_manager.upsert_player("g1", "u1", "Alice")
        assert "未设定" in await service.get_leaderboard_text("g1", 10, 0)

        await service.set_class("g1", "u1", "Alice", "法师")
        text = await service.get_leaderboard_text("g1", 10, 0)
        assert "[法师]" in text
        assert "[未设定]" not in text, "职业变更后不该还显示旧的占位职业"

    async def test_pilgrimage_cache_uses_same_ttl(self, db_manager):
        calls = {"n": 0}
        original = db_manager.get_top_players_by_pilgrimage

        async def counting(*args, **kwargs):
            calls["n"] += 1
            return await original(*args, **kwargs)

        db_manager.get_top_players_by_pilgrimage = counting
        service = LadderService(db_manager, ttl_getter=lambda: 0)
        await service.get_pilgrimage_leaderboard_text("g1", 10)
        await service.get_pilgrimage_leaderboard_text("g1", 10)
        assert calls["n"] == 2
