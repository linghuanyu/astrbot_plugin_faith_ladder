"""
Tests for the image renderer service.
"""

import pytest
from unittest.mock import Mock
from pathlib import Path

from astrbot_plugin_faith_ladder.image_renderer import ImageRenderer
from astrbot_plugin_faith_ladder.models import Player


class TestImageRenderer:
    """Tests for ImageRenderer (PIL-based local rendering)."""

    @pytest.fixture
    def renderer(self):
        """Create an ImageRenderer instance."""
        return ImageRenderer()

    @pytest.fixture
    def sample_players(self):
        """Sample player data."""
        return [
            Player(
                player_id="u1", group_id="g1", player_name="Alice",
                class_="法师", faith="存在",
                ladder_score=1000, pilgrimage_score=200
            ),
            Player(
                player_id="u2", group_id="g1", player_name="Bob",
                class_="战士", faith="虚无",
                ladder_score=800, pilgrimage_score=500
            ),
        ]

    @pytest.mark.asyncio
    async def test_render_leaderboard_returns_bytes(self, renderer, sample_players):
        """Test that rendering returns PNG bytes."""
        result = await renderer.render_leaderboard_image(sample_players, limit=10)

        assert result is not None
        assert isinstance(result, bytes)
        # PNG magic bytes
        assert result[:4] == b'\x89PNG'

    @pytest.mark.asyncio
    async def test_render_pilgrimage_returns_bytes(self, renderer, sample_players):
        """Test that pilgrimage rendering returns PNG bytes."""
        result = await renderer.render_pilgrimage_image(sample_players, limit=10)

        assert result is not None
        assert isinstance(result, bytes)
        assert result[:4] == b'\x89PNG'

    @pytest.mark.asyncio
    async def test_render_respects_limit(self, renderer, sample_players):
        """Test that display respects the limit parameter."""
        result = await renderer.render_leaderboard_image(sample_players, limit=1)

        assert result is not None
        assert isinstance(result, bytes)
        # Image should be smaller (only 1 player row)
        result_full = await renderer.render_leaderboard_image(sample_players, limit=10)
        assert len(result) < len(result_full)

    @pytest.mark.asyncio
    async def test_render_empty_players(self, renderer):
        """Test rendering with empty player list."""
        result = await renderer.render_leaderboard_image([], limit=10)

        assert result is not None
        assert isinstance(result, bytes)
        # Should still produce a valid image (empty state)
        assert result[:4] == b'\x89PNG'

    @pytest.mark.asyncio
    async def test_render_single_player(self, renderer, sample_players):
        """Test rendering with a single player."""
        result = await renderer.render_leaderboard_image(sample_players[:1], limit=10)

        assert result is not None
        assert isinstance(result, bytes)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_render_many_players(self, renderer):
        """Test rendering with many players."""
        players = [
            Player(
                player_id=f"u{i}", group_id="g1", player_name=f"Player{i:02d}",
                class_="法师", faith="存在",
                ladder_score=1000 - i * 50, pilgrimage_score=200 + i * 10
            )
            for i in range(10)
        ]
        result = await renderer.render_leaderboard_image(players, limit=10)

        assert result is not None
        assert isinstance(result, bytes)

    @pytest.mark.asyncio
    async def test_render_player_with_none_class_faith(self, renderer):
        """Test rendering with players that have no class/faith set."""
        players = [
            Player(player_id="u1", group_id="g1", player_name="Newbie",
                   ladder_score=500, pilgrimage_score=100)
        ]
        result = await renderer.render_leaderboard_image(players, limit=10)

        assert result is not None
        assert isinstance(result, bytes)

    @pytest.mark.asyncio
    async def test_ladder_vs_pilgrimage_different_output(self, renderer, sample_players):
        """Test that ladder and pilgrimage produce different images."""
        ladder_bytes = await renderer.render_leaderboard_image(sample_players, limit=10)
        pilgrimage_bytes = await renderer.render_pilgrimage_image(sample_players, limit=10)

        assert ladder_bytes != pilgrimage_bytes
