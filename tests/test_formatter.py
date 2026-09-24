"""
Tests for the message formatter.
"""

import pytest
from astrbot_plugin_faith_ladder.models import Player
from astrbot_plugin_faith_ladder.message_formatter import (
    format_leaderboard,
    format_pilgrimage_leaderboard,
    format_player_card,
    format_help,
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
        assert "前 3 名" in result
        # Should only show 3 numbered entries
        assert "4." not in result

    def test_unset_class_and_faith(self):
        """Test display with unset class/faith."""
        player = Player(player_id="u1", group_id="g1", player_name="Alice")
        result = format_leaderboard([player], 10)
        assert "[未设定]" in result
        assert "<未设定>" in result


class TestPilgrimageChosenMark:
    """觐见之梯榜首的「神选？」标记（原文用词，带问号是原著那句本身就是疑问句）。"""

    def test_top_player_is_marked(self):
        players = [
            Player(player_id="u1", group_id="g1", player_name="Alice", pilgrimage_score=200),
            Player(player_id="u2", group_id="g1", player_name="Bob", pilgrimage_score=100),
        ]
        result = format_pilgrimage_leaderboard(players, 10)
        assert "1. 神选？ Alice" in result
        # 只有榜首带这个标记
        assert "2. Bob" in result
        assert "神选？ Bob" not in result

    def test_ladder_leaderboard_is_not_marked(self):
        """标记只属于觐见之梯，登神之路榜首不加。"""
        players = [Player(player_id="u1", group_id="g1", player_name="Alice", ladder_score=200)]
        assert "神选？" not in format_leaderboard(players, 10)

    def test_empty_leaderboard_unaffected(self):
        assert format_pilgrimage_leaderboard([], 10) == "暂无排名数据。"


class TestFormatPlayerCard:
    """Tests for player card formatting."""

    def test_default_card(self):
        """Test card with default values (initial scores = not ranked)."""
        player = Player(player_id="u1", group_id="g1", player_name="TestPlayer")
        result = format_player_card(player)
        assert "姓名: TestPlayer" in result
        assert "职业: 未设定" in result
        assert "信仰：未设定" in result
        assert "登神之路: 0" in result
        assert "觐见之梯:  0" in result
        assert "未上榜" in result

    def test_full_card(self):
        """Test card with all values set."""
        player = Player(
            player_id="u1", group_id="g1", player_name="TestPlayer",
            class_="战士", faith="虚无",
            ladder_score=500, pilgrimage_score=200
        )
        result = format_player_card(player, ladder_rank=3, pilgrimage_rank=1)
        assert "职业: 战士" in result
        assert "信仰：虚无" in result
        assert "登神之路: 500（第3名）" in result
        assert "觐见之梯:  200（第1名）" in result

    def test_initial_scores_not_ranked(self):
        """Test that initial scores (configurable) show as not ranked."""
        player = Player(
            player_id="u1", group_id="g1", player_name="TestPlayer",
            ladder_score=1000, pilgrimage_score=100
        )
        result = format_player_card(player, ladder_rank=5, pilgrimage_rank=3)
        assert "未上榜" in result

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
        assert "未上榜" in result


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


class TestFormatHelpOathCommands:
    """帮助里的立誓/弃誓指令名必须来自配置。

    此前这两行写死「立誓」「弃誓」，而 schema 里明明有 cmd_take_oath / cmd_abandon_oath——
    群主改了指令名后，帮助文案与实际可用的指令对不上。
    """

    def test_uses_configured_names(self):
        text = format_help({"cmd_take_oath": "oath", "cmd_abandon_oath": "break"})
        assert "oath <玩家名>" in text
        assert "break <玩家名>" in text
        assert "立誓 <玩家名>" not in text
        assert "弃誓 <玩家名>" not in text

    def test_defaults_when_config_missing(self):
        text = format_help({})
        assert "立誓 <玩家名>" in text
        assert "弃誓 <玩家名>" in text


class TestFormatHelpInitScores:
    """帮助里的"初始之位"必须来自配置。

    此前硬编码 "登神之路 1000 · 觐见之梯 100"，群主改过初始分后，
    帮助文案与实际重置结果不一致。
    """

    def test_uses_configured_init_scores(self):
        text = format_help({"init_ladder_score": 777, "init_pilgrimage_score": 88})
        assert "登神之路 777" in text
        assert "觐见之梯 88" in text

    def test_defaults_when_config_missing(self):
        text = format_help({})
        assert "登神之路 1000" in text
        assert "觐见之梯 100" in text
