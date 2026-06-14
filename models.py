"""
Data models for the faith ladder plugin.
"""

from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime


# Valid character classes and faiths
VALID_CLASSES = ["战士", "牧师", "猎人", "法师", "歌者", "刺客"]
VALID_FAITHS = ["虚无", "存在", "文明", "沉沦", "混沌","生命"]


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

    @property
    def total_score(self) -> int:
        """Total score across both ladders."""
        return self.ladder_score + self.pilgrimage_score

    def is_valid_class(self) -> bool:
        """Check if the current class is valid."""
        return self.class_ in VALID_CLASSES

    def is_valid_faith(self) -> bool:
        """Check if the current faith is valid."""
        return self.faith in VALID_FAITHS

    @staticmethod
    def validate_class(class_name: str) -> bool:
        """Validate a class name."""
        return class_name in VALID_CLASSES

    @staticmethod
    def validate_faith(faith_name: str) -> bool:
        """Validate a faith name."""
        return faith_name in VALID_FAITHS


@dataclass
class ScoreEntry:
    """Represents a score change record."""
    player_id: str
    group_id: str
    ladder_change: int = 0
    pilgrimage_change: int = 0
    reason: Optional[str] = None
    operator_id: Optional[str] = None
    timestamp: Optional[str] = None
