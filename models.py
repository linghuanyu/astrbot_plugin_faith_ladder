"""
Data models for the faith ladder plugin.
"""

from dataclasses import dataclass
from typing import Optional


# Valid character classes and faiths
VALID_CLASSES = ["战士", "牧师", "猎人", "法师", "歌者", "刺客"]
VALID_FAITHS = ["虚无", "存在", "文明", "沉沦", "混沌", "生命"]


@dataclass
class Player:
    """Represents a player in the faith ladder system."""
    player_id: str
    group_id: str
    player_name: str
    class_: Optional[str] = None
    faith: Optional[str] = None
    ladder_score: int = 0
    pilgrimage_score: int = 0
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    oathbreaker: bool = False

    @staticmethod
    def validate_class(class_name: str) -> bool:
        """Validate a class name."""
        return class_name in VALID_CLASSES

    @staticmethod
    def validate_faith(faith_name: str) -> bool:
        """Validate a faith name."""
        return faith_name in VALID_FAITHS
