"""
进度感相关的纯计算：位阶徽记。

为什么单独一个模块：这是"给一个登神之路分数算出徽记"的纯函数，
不碰数据库、不依赖框架，可以直接单测；放在 ladder_service 里会和 SQL/缓存搅在一起。
"""

from __future__ import annotations

from typing import List

# ========== 位阶（徽记） ==========
# 按登神之路分数划分 9 阶，只显示符号徽记，不出现中文阶名。
# 阈值升序：index 0 是最低阶；分数低于第一个阈值时也归入最低阶。
DEFAULT_TIER_THRESHOLDS = [0, 1000, 1100, 1300, 1600, 2000, 2600, 3500, 5000]
DEFAULT_TIER_MARKS = ["☽", "☿", "♀", "♁", "♂", "♃", "♄", "♅", "☉"]


def _clean_thresholds(thresholds) -> List[int]:
    """阈值清洗：只保留整数并升序；不可用时回落默认表。"""
    values: List[int] = []
    for item in thresholds or []:
        try:
            values.append(int(item))
        except (TypeError, ValueError):
            continue
    values.sort()
    return values or list(DEFAULT_TIER_THRESHOLDS)


def _clean_marks(marks) -> List[str]:
    values = [str(m).strip() for m in (marks or []) if str(m).strip()]
    return values or list(DEFAULT_TIER_MARKS)


def tier_index(score: int, thresholds=None) -> int:
    """分数落在第几阶（0 基）。低于最低阈值归 0，高于最高阈值归最高阶。"""
    table = _clean_thresholds(thresholds)
    index = 0
    for i, floor_score in enumerate(table):
        if score >= floor_score:
            index = i
        else:
            break
    return index


def tier_mark(score: int, thresholds=None, marks=None) -> str:
    """分数对应的徽记；徽记数量少于阶数时，超出部分用最后一个。

    徽记与阈值数量不一致不算错误——管理员可以只给前几阶配符号，
    剩下的沿用最后一个，避免"少配一个就整个不显示"。
    """
    table = _clean_marks(marks)
    index = min(tier_index(score, thresholds), len(table) - 1)
    return table[index]


def build_tier_marks(players, thresholds=None, marks=None) -> dict:
    """给一组玩家算 {player_id: 徽记}，供榜单/卡片渲染。"""
    thresholds = _clean_thresholds(thresholds)
    marks_clean = _clean_marks(marks)
    return {
        getattr(p, "player_id", ""): tier_mark(getattr(p, "ladder_score", 0), thresholds, marks_clean)
        for p in players or []
    }
