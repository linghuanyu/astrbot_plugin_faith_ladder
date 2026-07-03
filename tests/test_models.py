"""
Tests for the data models.
"""

import pytest
from astrbot_plugin_faith_ladder.models import Player, VALID_CLASSES, VALID_FAITHS


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
        """Test faith validation with valid faiths."""
        for faith in VALID_FAITHS:
            assert Player.validate_faith(faith) is True

    def test_validate_faith_invalid(self):
        """Test faith validation with invalid faiths."""
        assert Player.validate_faith("无效信仰") is False
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

    def test_valid_faiths_list(self):
        """Test that VALID_FAITHS contains expected values."""
        assert "虚无" in VALID_FAITHS
        assert "存在" in VALID_FAITHS
        assert "文明" in VALID_FAITHS
        assert "沉沦" in VALID_FAITHS
        assert "混沌" in VALID_FAITHS
        assert "生命" in VALID_FAITHS
        assert len(VALID_FAITHS) == 6
