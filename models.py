"""
Data models for the faith ladder plugin.
"""

from dataclasses import dataclass
from typing import Optional


# Valid character classes
VALID_CLASSES = ["战士", "牧师", "猎人", "法师", "歌者", "刺客"]

# 命途（6个）：最高层级分类
VALID_PATHS = ["虚无", "存在", "文明", "沉沦", "混沌", "生命"]

# 信仰（16个）：每个命途下的具体信仰
VALID_FAITHS = [
    # 生命命途
    "诞育", "繁荣", "死亡",
    # 沉沦命途
    "污堕", "腐朽", "湮灭",
    # 文明命途
    "秩序", "真理", "战争",
    # 混沌命途
    "混乱", "痴愚", "沉默",
    # 存在命途
    "记忆", "时间",
    # 虚无命途
    "欺诈", "命运",
]

# 信仰 → 命途 映射
FAITH_TO_PATH = {
    "诞育": "生命", "繁荣": "生命", "死亡": "生命",
    "污堕": "沉沦", "腐朽": "沉沦", "湮灭": "沉沦",
    "秩序": "文明", "真理": "文明", "战争": "文明",
    "混乱": "混沌", "痴愚": "混沌", "沉默": "混沌",
    "记忆": "存在", "时间": "存在",
    "欺诈": "虚无", "命运": "虚无",
}


@dataclass
class Player:
    """Represents a player in the faith ladder system."""
    player_id: str
    group_id: str
    player_name: str
    class_: Optional[str] = None
    faith: Optional[str] = None  # 存储的是命途（path），如"生命"、"虚无"
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
        """Validate a faith name (现在是命途)."""
        return faith_name in VALID_PATHS
