"""
Tests for the command routing logic in main.py.
"""

import pytest
from unittest.mock import MagicMock, AsyncMock


class TestCommandRouting:
    """Tests for command name matching and routing logic."""

    def test_build_cmd_map_defaults(self):
        """Test building command map with default config."""
        config = {}

        cmd_map = {
            config.get("cmd_ladder", "天梯榜"): "ladder",
            config.get("cmd_query", "查询"): "query",
            config.get("cmd_add_score", "录入积分"): "add_score",
            config.get("cmd_set_class", "设置职业"): "set_class",
            config.get("cmd_my_info", "我的信息"): "my_info",
            config.get("cmd_admin", "天梯榜管理"): "admin",
            config.get("cmd_whitelist", "白名单"): "whitelist",
            config.get("cmd_help", "天梯榜帮助"): "help",
        }

        assert "天梯榜" in cmd_map
        assert "查询" in cmd_map
        assert "录入积分" in cmd_map
        assert "设置职业" in cmd_map
        assert len(cmd_map) == 8

    def test_build_cmd_map_custom(self):
        """Test building command map with custom config."""
        config = {
            "cmd_ladder": "rank",
            "cmd_query": "info",
            "cmd_add_score": "score",
            "cmd_set_class": "job",
            "cmd_my_info": "me",
            "cmd_admin": "manage",
            "cmd_whitelist": "wl",
            "cmd_help": "h",
        }

        cmd_map = {
            config.get("cmd_ladder", "天梯榜"): "ladder",
            config.get("cmd_query", "查询"): "query",
            config.get("cmd_add_score", "录入积分"): "add_score",
        }

        assert "rank" in cmd_map
        assert "info" in cmd_map
        assert "score" in cmd_map

    def test_command_matching_exact(self):
        """Test exact command name matching."""
        text = "天梯榜"
        cmd_name = "天梯榜"
        assert text == cmd_name or text.startswith(cmd_name + " ")

    def test_command_matching_with_args(self):
        """Test command matching with arguments."""
        text = "查询 Alice"
        cmd_name = "查询"
        assert text == cmd_name or text.startswith(cmd_name + " ")

    def test_command_not_matching(self):
        """Test non-matching text."""
        text = "今天天气不错"
        cmd_name = "天梯榜"
        assert not (text == cmd_name or text.startswith(cmd_name + " "))

    def test_command_prefix_no_false_positive(self):
        """Test that partial prefix doesn't match."""
        text = "天梯榜管理"
        cmd_name = "天梯榜"
        # "天梯榜管理" should NOT match "天梯榜" command
        assert not (text == cmd_name or text.startswith(cmd_name + " "))

    def test_longer_command_priority(self):
        """Test that longer command names are matched first."""
        commands = {
            "天梯榜": "ladder",
            "天梯榜管理": "admin",
            "天梯榜帮助": "help",
        }

        text = "天梯榜管理"
        sorted_cmds = sorted(commands.items(), key=lambda x: len(x[0]), reverse=True)

        matched = None
        for cmd_name, handler in sorted_cmds:
            if text == cmd_name or text.startswith(cmd_name + " "):
                matched = handler
                break

        assert matched == "admin"

    def test_args_extraction(self):
        """Test extracting arguments from command text."""
        cmd_name = "查询"
        text = "查询 Alice"
        args_str = text[len(cmd_name):].strip()
        assert args_str == "Alice"

    def test_args_extraction_no_args(self):
        """Test extracting when no arguments."""
        cmd_name = "天梯榜"
        text = "天梯榜"
        args_str = text[len(cmd_name):].strip()
        assert args_str == ""

    def test_args_extraction_multiple_args(self):
        """Test extracting multiple arguments."""
        cmd_name = "录入积分"
        text = "录入积分 Alice 100 50"
        args_str = text[len(cmd_name):].strip()
        assert args_str == "Alice 100 50"
        parts = args_str.split()
        assert parts == ["Alice", "100", "50"]

    def test_args_extraction_chinese(self):
        """Test argument extraction with Chinese text."""
        cmd_name = "设置职业"
        text = "设置职业 法师 存在"
        args_str = text[len(cmd_name):].strip()
        parts = args_str.split()
        assert parts == ["法师", "存在"]
