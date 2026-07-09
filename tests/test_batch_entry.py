"""
Tests for batch score entry functionality.
"""

import pytest
import tempfile
from pathlib import Path

from astrbot_plugin_faith_ladder.db_manager import DatabaseManager
from astrbot_plugin_faith_ladder.ladder_service import LadderService


class TestBatchScoreParsing:
    """Tests for parse_batch_scores()."""

    @pytest.fixture
    def service(self):
        return LadderService.__new__(LadderService)

    SAMPLE_TEXT = (
        "【正在评分，并结算奖励……】\n"
        "【玩家：阡陌 表现评分：A+】\n"
        "【获得道具：重点线（b）】\n"
        "【登神之路+16】\n"
        "【觐见之梯+2】\n"
        "【当前登神之路得分：1128】\n"
        "【当前觐见之梯得分：115】\n"
        "【玩家：惜字如金 表现评分：S】\n"
        "【获得道具：噤声骨刃（b）】\n"
        "【登神之路+18】\n"
        "【觐见之梯+2】\n"
        "【玩家：胡斌 评分：A+】\n"
        "【获得道具：无】\n"
        "【登神之路+14】\n"
        "【觐见之梯+2】\n"
        "【试炼通关，即将退出】"
    )

    def test_parse_sample_text(self, service):
        """Test parsing the exact sample text from user request."""
        results, err = service.parse_batch_scores(self.SAMPLE_TEXT)
        assert err is None
        assert len(results) == 3

        assert results[0]["name"] == "阡陌"
        assert results[0]["ladder_delta"] == 16
        assert results[0]["pilgrimage_delta"] == 2

        assert results[1]["name"] == "惜字如金"
        assert results[1]["ladder_delta"] == 18
        assert results[1]["pilgrimage_delta"] == 2

        assert results[2]["name"] == "胡斌"
        assert results[2]["ladder_delta"] == 14
        assert results[2]["pilgrimage_delta"] == 2

    def test_parse_ladder_only(self, service):
        """Test text with only ladder score."""
        text = "【玩家：Alice】【登神之路+20】"
        results, err = service.parse_batch_scores(text)
        assert err is None
        assert results[0]["ladder_delta"] == 20
        assert results[0]["pilgrimage_delta"] == 0

    def test_parse_pilgrimage_only(self, service):
        """Test text with only pilgrimage score."""
        text = "【玩家：Bob】【觐见之梯+3】"
        results, err = service.parse_batch_scores(text)
        assert err is None
        assert results[0]["ladder_delta"] == 0
        assert results[0]["pilgrimage_delta"] == 3

    def test_parse_empty_text(self, service):
        """Test parsing empty text returns error."""
        results, err = service.parse_batch_scores("")
        assert err is not None
        assert results == []

    def test_parse_no_valid_data(self, service):
        """Test parsing text with no valid score data."""
        text = "这是一段无关的文本"
        results, err = service.parse_batch_scores(text)
        assert err is not None

    def test_parse_colon_variants(self, service):
        """Test both full-width and half-width colons."""
        text1 = "【玩家：Alice】【登神之路+5】"
        text2 = "【玩家:Bob】【登神之路+5】"
        r1, _ = service.parse_batch_scores(text1)
        r2, _ = service.parse_batch_scores(text2)
        assert r1[0]["name"] == "Alice"
        assert r2[0]["name"] == "Bob"

    def test_parse_space_after_plus(self, service):
        """Test that spaces after + are handled."""
        text = "【玩家：Alice】【登神之路+ 10】【觐见之梯+ 3】"
        results, err = service.parse_batch_scores(text)
        assert err is None
        assert results[0]["ladder_delta"] == 10
        assert results[0]["pilgrimage_delta"] == 3

    def test_parse_negative_ladder_score(self, service):
        """Test parsing negative ladder score (e.g., 登神之路-3)."""
        text = (
            "【玩家：幾點，表现评分：C】\n"
            "【获得道具：无】\n"
            "【登神之路-3】\n"
            "【觐见之梯+1】\n"
        )
        results, err = service.parse_batch_scores(text)
        assert err is None
        assert len(results) == 1
        assert results[0]["name"] == "幾點"
        assert results[0]["ladder_delta"] == -3
        assert results[0]["pilgrimage_delta"] == 1

    def test_parse_negative_both_scores(self, service):
        """Test parsing both negative scores."""
        text = "【玩家：Test】【登神之路-10】【觐见之梯-5】"
        results, err = service.parse_batch_scores(text)
        assert err is None
        assert results[0]["ladder_delta"] == -10
        assert results[0]["pilgrimage_delta"] == -5

    def test_parse_mixed_positive_negative(self, service):
        """Test mixed positive and negative across players."""
        text = (
            "【玩家：Alice】【登神之路+16】【觐见之梯+2】\n"
            "【玩家：Bob】【登神之路-3】【觐见之梯+1】\n"
        )
        results, err = service.parse_batch_scores(text)
        assert err is None
        assert len(results) == 2
        assert results[0]["ladder_delta"] == 16
        assert results[1]["ladder_delta"] == -3

    def test_parse_bracket_name(self, service):
        """Test player name wrapped in brackets like 【吃鱼】."""
        text = "【玩家：【吃鱼】表现评分：A+】【登神之路+16】【觐见之梯+2】"
        results, err = service.parse_batch_scores(text)
        assert err is None
        assert len(results) == 1
        assert results[0]["name"] == "吃鱼"
        assert results[0]["ladder_delta"] == 16
        assert results[0]["pilgrimage_delta"] == 2

    def test_parse_mixed_bracket_and_plain_names(self, service):
        """Test mix of bracket names and plain names."""
        text = (
            "【玩家：【吃鱼】表现评分：S】【登神之路+18】【觐见之梯+2】\n"
            "【玩家：Alice 表现评分：A】【登神之路+10】【觐见之梯+1】\n"
            "【玩家：幾點，表现评分：C】【登神之路-3】【觐见之梯+1】\n"
        )
        results, err = service.parse_batch_scores(text)
        assert err is None
        assert len(results) == 3
        assert results[0]["name"] == "吃鱼"
        assert results[1]["name"] == "Alice"
        assert results[2]["name"] == "幾點"


class TestBatchScoreDB:
    """Integration tests for batch_add_scores() with real DB."""

    @pytest.fixture
    async def service(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = DatabaseManager(Path(tmpdir))
            await db.initialize()
            svc = LadderService(db)
            yield svc
            await db.close()

    @pytest.mark.asyncio
    async def test_batch_add_scores(self, service):
        """Test batch adding scores to existing players."""
        # Create players
        await service.db.upsert_player("g1", "u1", "Alice")
        await service.db.upsert_player("g1", "u2", "Bob")

        parsed = [
            {"name": "Alice", "ladder_delta": 16, "pilgrimage_delta": 2},
            {"name": "Bob", "ladder_delta": 18, "pilgrimage_delta": 2},
        ]
        success_count, details, skipped = await service.batch_add_scores("g1", parsed, "op1")

        assert success_count == 2
        assert skipped == []

        # Verify scores updated
        alice = await service.db.get_player_by_name("g1", "Alice")
        assert alice.ladder_score == 1016  # 1000 + 16
        assert alice.pilgrimage_score == 102  # 100 + 2

    @pytest.mark.asyncio
    async def test_batch_add_skips_missing(self, service):
        """Test that batch add skips non-existent players."""
        await service.db.upsert_player("g1", "u1", "Alice")

        parsed = [
            {"name": "Alice", "ladder_delta": 10, "pilgrimage_delta": 5},
            {"name": "Missing", "ladder_delta": 20, "pilgrimage_delta": 3},
        ]
        success_count, details, skipped = await service.batch_add_scores("g1", parsed, "op1")

        assert success_count == 1
        assert "Missing" in skipped

    @pytest.mark.asyncio
    async def test_batch_add_empty_list(self, service):
        """Test batch add with empty list."""
        success_count, details, skipped = await service.batch_add_scores("g1", [], "op1")
        assert success_count == 0
        assert skipped == []
