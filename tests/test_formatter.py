"""
Tests for the message formatter.
"""

import pytest
from astrbot_plugin_faith_ladder.models import Player
from astrbot_plugin_faith_ladder.message_formatter import (
    format_leaderboard,
    format_player_card,
    format_help,
    format_whitelist,
    format_score_result,
)


class TestFormatLeaderboard:
    """Tests for leaderboard formatting."""

    def test_empty_leaderboard(self):
        """Test formatting empty leaderboard."""
        result = format_leaderboard([], 10)
        assert result == "暂无排名数据。"

    def test_single_player(self):
        """Test formatting single player."""
        player = Player(
            player_id="u1", group_id="g1", player_name="Alice",
            class_="法师", faith="存在",
            ladder_score=100, pilgrimage_score=50
        )
        result = format_leaderboard([player], 10)
        assert "1. Alice" in result
        assert "[法师]" in result
        assert "<存在>" in result
        assert "登神之路: 100" in result
        assert "觐见之梯: 50" in result

    def test_multiple_players_sorted(self):
        """Test formatting multiple players (should display in given order)."""
        # Players should be pre-sorted by caller (DB returns sorted)
        players = [
            Player(player_id="u2", group_id="g1", player_name="Bob", ladder_score=200, pilgrimage_score=20),
            Player(player_id="u1", group_id="g1", player_name="Alice", ladder_score=100, pilgrimage_score=10),
        ]
        result = format_leaderboard(players, 10)
        assert "1. Bob" in result
        assert "2. Alice" in result

    def test_leaderboard_limit(self):
        """Test that limit is respected in display."""
        players = [
            Player(player_id=f"u{i}", group_id="g1", player_name=f"P{i}", ladder_score=i*10)
            for i in range(10)
        ]
        result = format_leaderboard(players, 3)
        assert "显示前 3 名" in result
        # Should only show 3 numbered entries
        assert "4." not in result

    def test_unset_class_and_faith(self):
        """Test display with unset class/faith."""
        player = Player(player_id="u1", group_id="g1", player_name="Alice")
        result = format_leaderboard([player], 10)
        assert "[未设定]" in result
        assert "<未设定>" in result


class TestFormatPlayerCard:
    """Tests for player card formatting."""

    def test_default_card(self):
        """Test card with default values (initial scores = not ranked)."""
        player = Player(player_id="u1", group_id="g1", player_name="TestPlayer")
        result = format_player_card(player)
        assert "姓名: TestPlayer" in result
        assert "职业: 未设定" in result
        assert "信仰: 未设定" in result
        assert "登神之路: 0" in result
        assert "觐见之梯: 0" in result
        assert "登神之路排名: 未上榜" in result
        assert "觐见之梯排名: 未上榜" in result

    def test_full_card(self):
        """Test card with all values set."""
        player = Player(
            player_id="u1", group_id="g1", player_name="TestPlayer",
            class_="战士", faith="虚无",
            ladder_score=500, pilgrimage_score=200
        )
        result = format_player_card(player, ladder_rank=3, pilgrimage_rank=1)
        assert "职业: 战士" in result
        assert "信仰: 虚无" in result
        assert "登神之路: 500" in result
        assert "觐见之梯: 200" in result
        assert "登神之路排名: 第3名" in result
        assert "觐见之梯排名: 第1名" in result

    def test_initial_scores_not_ranked(self):
        """Test that initial scores (configurable) show as not ranked."""
        player = Player(
            player_id="u1", group_id="g1", player_name="TestPlayer",
            ladder_score=1000, pilgrimage_score=100
        )
        result = format_player_card(player, ladder_rank=5, pilgrimage_rank=3)
        assert "登神之路排名: 未上榜" in result
        assert "觐见之梯排名: 未上榜" in result

    def test_custom_initial_scores(self):
        """Test with custom initial score thresholds."""
        player = Player(
            player_id="u1", group_id="g1", player_name="TestPlayer",
            ladder_score=500, pilgrimage_score=50
        )
        result = format_player_card(
            player, ladder_rank=5, pilgrimage_rank=3,
            init_ladder=500, init_pilgrimage=50
        )
        assert "登神之路排名: 未上榜" in result
        assert "觐见之梯排名: 未上榜" in result


class TestFormatHelp:
    """Tests for help message formatting."""

    def test_help_with_default_commands(self):
        """Test help with default command names."""
        config = {
            "cmd_ladder": "天梯榜",
            "cmd_query": "查询",
            "cmd_add_score": "录入积分",
            "cmd_set_class": "设置职业",
            "cmd_admin": "天梯榜管理",
            "cmd_whitelist": "白名单",
            "cmd_help": "天梯榜帮助",
        }
        result = format_help(config)
        assert "天梯榜" in result
        assert "查询" in result
        assert "设置职业" in result
        assert "战士" in result
        assert "牧师" in result

    def test_help_with_custom_commands(self):
        """Test help with custom command names."""
        config = {
            "cmd_ladder": "rank",
            "cmd_query": "info",
            "cmd_set_class": "job",
            "cmd_add_score": "score",
            "cmd_admin": "manage",
            "cmd_whitelist": "wl",
            "cmd_help": "h",
        }
        result = format_help(config)
        assert "rank" in result
        assert "info" in result
        assert "job" in result

    def test_help_shows_all_classes(self):
        """Test that help lists all valid classes."""
        config = {}
        result = format_help(config)
        assert "战士" in result
        assert "牧师" in result
        assert "猎人" in result
        assert "法师" in result
        assert "歌者" in result

    def test_help_shows_all_faiths(self):
        """Test that help lists all valid faiths."""
        config = {}
        result = format_help(config)
        assert "虚无" in result
        assert "存在" in result
        assert "文明" in result
        assert "沉沦" in result
        assert "混沌" in result

    def test_help_shows_output_mode_text(self):
        """Test that help shows text output mode."""
        config = {"output_mode": "text"}
        result = format_help(config)
        assert "输出模式" in result
        assert "text" in result

    def test_help_shows_output_mode_image(self):
        """Test that help shows image output mode."""
        config = {"output_mode": "image"}
        result = format_help(config)
        assert "输出模式" in result
        assert "image" in result

    def test_help_shows_default_output_mode(self):
        """Test that help shows default text output mode when not configured."""
        config = {}
        result = format_help(config)
        assert "输出模式" in result
        assert "text" in result


class TestFormatWhitelist:
    """Tests for whitelist formatting."""

    def test_empty_whitelist(self):
        """Test formatting empty whitelist."""
        result = format_whitelist([])
        assert result == "白名单为空。"

    def test_whitelist_with_entries(self):
        """Test formatting whitelist with entries."""
        entries = [
            {"entry_type": "user", "entry_id": "u123", "added_by": "admin", "added_at": "2024-01-01"},
            {"entry_type": "group", "entry_id": "g456", "added_by": "admin", "added_at": "2024-01-02"},
        ]
        result = format_whitelist(entries)
        assert "[user] u123" in result
        assert "[group] g456" in result
        assert "共 2 条记录" in result


class TestFormatScoreResult:
    """Tests for score result formatting."""

    def test_positive_scores(self):
        """Test formatting with positive score changes."""
        result = format_score_result("Alice", 100, 50, 200, 100)
        assert "Alice" in result
        assert "+100" in result
        assert "+50" in result
        assert "200" in result
        assert "100" in result

    def test_negative_scores(self):
        """Test formatting with negative score changes."""
        result = format_score_result("Bob", -30, -20, 70, 80)
        assert "-30" in result
        assert "-20" in result
        assert "70" in result
        assert "80" in result
