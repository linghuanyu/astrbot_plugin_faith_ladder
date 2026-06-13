"""
Pytest configuration and shared fixtures.
"""

import pytest
import pytest_asyncio
import asyncio
import tempfile
from pathlib import Path

import sys
import os

# Add the project root (parent of plugin package) to sys.path
_plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_project_root = os.path.dirname(_plugin_dir)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
if _plugin_dir not in sys.path:
    sys.path.insert(1, _plugin_dir)


pytest_plugins = ['pytest_asyncio']


@pytest.fixture
def temp_data_dir(tmp_path):
    """Provide a temporary data directory for tests."""
    data_dir = tmp_path / "test_plugin_data"
    data_dir.mkdir()
    return data_dir


@pytest_asyncio.fixture
async def db_manager(temp_data_dir):
    """Provide an initialized DatabaseManager for tests."""
    from astrbot_plugin_faith_ladder.db_manager import DatabaseManager
    db = DatabaseManager(temp_data_dir)
    await db.initialize()
    yield db
    await db.close()


@pytest.fixture
def sample_player_data():
    """Sample player data for tests."""
    return {
        "group_id": "group_001",
        "player_id": "user_123",
        "player_name": "测试玩家",
        "class_": "战士",
        "faith": "存在",
    }


@pytest.fixture
def sample_players():
    """Multiple sample players for leaderboard tests."""
    from astrbot_plugin_faith_ladder.models import Player
    return [
        Player(player_id="u1", group_id="g1", player_name="Alice", class_="法师", faith="存在", ladder_score=1000, pilgrimage_score=200),
        Player(player_id="u2", group_id="g1", player_name="Bob", class_="战士", faith="虚无", ladder_score=800, pilgrimage_score=500),
        Player(player_id="u3", group_id="g1", player_name="Charlie", class_="猎人", faith="文明", ladder_score=1200, pilgrimage_score=100),
        Player(player_id="u4", group_id="g1", player_name="Diana", class_="牧师", faith="沉沦", ladder_score=600, pilgrimage_score=300),
        Player(player_id="u5", group_id="g1", player_name="Eve", class_="歌者", faith="混沌", ladder_score=900, pilgrimage_score=400),
    ]
