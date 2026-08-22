"""
Tests for the data models.
"""

import pytest
from astrbot_plugin_faith_ladder.models import Player, VALID_CLASSES, VALID_PATHS, VALID_FAITHS, FAITH_TO_PATH


class TestPlayerModel:
    """Tests for the Player data model."""

    def test_create_player_defaults(self):
        """Test creating a player with default values."""
        player = Player(player_id="u1", group_id="g1", player_name="Test")
        assert player.player_id == "u1"
        assert player.group_id == "g1"
        assert player.player_name == "Test"
        assert player.class_ is None
        assert player.faith is None
        assert player.ladder_score == 0
        assert player.pilgrimage_score == 0

    def test_create_player_full(self):
        """Test creating a player with all fields."""
        player = Player(
            player_id="u1", group_id="g1", player_name="Test",
            class_="法师", faith="存在",
            ladder_score=100, pilgrimage_score=50
        )
        assert player.class_ == "法师"
        assert player.faith == "存在"
        assert player.ladder_score == 100
        assert player.pilgrimage_score == 50

    def test_validate_class_valid(self):
        """Test class validation with valid classes."""
        for cls in VALID_CLASSES:
            assert Player.validate_class(cls) is True

    def test_validate_class_invalid(self):
        """Test class validation with invalid classes."""
        assert Player.validate_class("无效职业") is False
        assert Player.validate_class("") is False
        assert Player.validate_class("warrior") is False

    def test_validate_faith_valid(self):
        """Test faith (命途) validation with valid paths."""
        for path in VALID_PATHS:
            assert Player.validate_faith(path) is True

    def test_validate_faith_invalid(self):
        """Test faith (命途) validation with invalid paths."""
        assert Player.validate_faith("无效命途") is False
        assert Player.validate_faith("") is False
        assert Player.validate_faith("existence") is False

    def test_valid_classes_list(self):
        """Test that VALID_CLASSES contains expected values."""
        assert "战士" in VALID_CLASSES
        assert "牧师" in VALID_CLASSES
        assert "猎人" in VALID_CLASSES
        assert "法师" in VALID_CLASSES
        assert "歌者" in VALID_CLASSES
        assert "刺客" in VALID_CLASSES
        assert len(VALID_CLASSES) == 6

    def test_valid_paths_list(self):
        """Test that VALID_PATHS (命途) contains expected values."""
        assert "虚无" in VALID_PATHS
        assert "存在" in VALID_PATHS
        assert "文明" in VALID_PATHS
        assert "沉沦" in VALID_PATHS
        assert "混沌" in VALID_PATHS
        assert "生命" in VALID_PATHS
        assert len(VALID_PATHS) == 6

    def test_valid_faiths_list(self):
        """Test that VALID_FAITHS (具体信仰) contains expected values."""
        # 生命命途下的信仰
        assert "诞育" in VALID_FAITHS
        assert "繁荣" in VALID_FAITHS
        assert "死亡" in VALID_FAITHS
        # 虚无命途下的信仰
        assert "欺诈" in VALID_FAITHS
        assert "命运" in VALID_FAITHS
        assert len(VALID_FAITHS) == 16

    def test_faith_to_path_mapping(self):
        """Test that FAITH_TO_PATH correctly maps faiths to paths."""
        assert FAITH_TO_PATH["诞育"] == "生命"
        assert FAITH_TO_PATH["繁荣"] == "生命"
        assert FAITH_TO_PATH["死亡"] == "生命"
        assert FAITH_TO_PATH["欺诈"] == "虚无"
        assert FAITH_TO_PATH["命运"] == "虚无"
        assert FAITH_TO_PATH["秩序"] == "文明"
        assert len(FAITH_TO_PATH) == 16