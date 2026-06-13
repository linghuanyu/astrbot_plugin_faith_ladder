"""
Tests for the data models.
"""

import pytest
from astrbot_plugin_faith_ladder.models import Player, ScoreEntry, VALID_CLASSES, VALID_FAITHS


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

    def test_total_score(self):
        """Test total score calculation."""
        player = Player(
            player_id="u1", group_id="g1", player_name="Test",
            ladder_score=100, pilgrimage_score=50
        )
        assert player.total_score == 150

    def test_total_score_zeros(self):
        """Test total score with zero values."""
        player = Player(player_id="u1", group_id="g1", player_name="Test")
        assert player.total_score == 0

    def test_total_score_negative(self):
        """Test total score with negative values."""
        player = Player(
            player_id="u1", group_id="g1", player_name="Test",
            ladder_score=-50, pilgrimage_score=200
        )
        assert player.total_score == 150

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

    def test_is_valid_class_instance(self):
        """Test instance method for class validation."""
        player = Player(player_id="u1", group_id="g1", player_name="Test", class_="战士")
        assert player.is_valid_class() is True

        player2 = Player(player_id="u2", group_id="g1", player_name="Test2", class_="invalid")
        assert player2.is_valid_class() is False

    def test_is_valid_class_none(self):
        """Test is_valid_class when class is None."""
        player = Player(player_id="u1", group_id="g1", player_name="Test")
        assert player.is_valid_class() is False

    def test_is_valid_faith_instance(self):
        """Test instance method for faith validation."""
        player = Player(player_id="u1", group_id="g1", player_name="Test", faith="虚无")
        assert player.is_valid_faith() is True

    def test_is_valid_faith_none(self):
        """Test is_valid_faith when faith is None."""
        player = Player(player_id="u1", group_id="g1", player_name="Test")
        assert player.is_valid_faith() is False

    def test_valid_classes_list(self):
        """Test that VALID_CLASSES contains expected values."""
        assert "战士" in VALID_CLASSES
        assert "牧师" in VALID_CLASSES
        assert "猎人" in VALID_CLASSES
        assert "法师" in VALID_CLASSES
        assert "歌者" in VALID_CLASSES
        assert len(VALID_CLASSES) == 5

    def test_valid_faiths_list(self):
        """Test that VALID_FAITHS contains expected values."""
        assert "虚无" in VALID_FAITHS
        assert "存在" in VALID_FAITHS
        assert "文明" in VALID_FAITHS
        assert "沉沦" in VALID_FAITHS
        assert "混沌" in VALID_FAITHS
        assert "生命" in VALID_FAITHS
        assert len(VALID_FAITHS) == 6


class TestScoreEntryModel:
    """Tests for the ScoreEntry data model."""

    def test_create_score_entry_defaults(self):
        """Test creating a score entry with defaults."""
        entry = ScoreEntry(player_id="u1", group_id="g1")
        assert entry.player_id == "u1"
        assert entry.group_id == "g1"
        assert entry.ladder_change == 0
        assert entry.pilgrimage_change == 0
        assert entry.reason is None
        assert entry.operator_id is None

    def test_create_score_entry_full(self):
        """Test creating a score entry with all fields."""
        entry = ScoreEntry(
            player_id="u1", group_id="g1",
            ladder_change=100, pilgrimage_change=50,
            reason="测试", operator_id="admin_001"
        )
        assert entry.ladder_change == 100
        assert entry.pilgrimage_change == 50
        assert entry.reason == "测试"
        assert entry.operator_id == "admin_001"
