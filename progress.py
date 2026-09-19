"""
进度感相关的纯计算：试炼刻痕（里程碑）。

为什么单独一个模块：这些都是"给两个分数（有时加名次）算出一串事件"的纯函数，
不碰数据库、不依赖框架，可以直接单测；放在 ladder_service 里会和 SQL/缓存搅在一起。
"""

from __future__ import annotations

from typing import List, Optional, Tuple

# 刻痕类型
KIND_HUNDRED = "hundred"      # 跨过整百
KIND_THOUSAND = "thousand"    # 跨过整千
KIND_THRESHOLD = "threshold"  # 首次越过上榜门槛
KIND_RANK = "rank"            # 名次上升


def crossed_hundreds(old: int, new: int) -> int:
    """本次跨过的百位刻度数（0 = 没跨过）。"""
    if new <= old:
        return 0
    return new // 100 - old // 100


def crossed_thousands(old: int, new: int) -> int:
    """本次跨过的千位刻度数（0 = 没跨过）。"""
    if new <= old:
        return 0
    return new // 1000 - old // 1000


def crossed_threshold(old: int, new: int, threshold: int) -> bool:
    """是否**首次**越过门槛（门槛 <= 0 视为没有门槛）。"""
    if threshold <= 0:
        return False
    return old < threshold <= new


def detect_milestones(
    old_ladder: int,
    new_ladder: int,
    min_ladder_score: int = 0,
    rank_before: Optional[int] = None,
    rank_after: Optional[int] = None,
) -> List[Tuple[str, int]]:
    """算出本次分数变化触发的刻痕事件。

    返回 `[(kind, value)]`，value 的含义按 kind 不同：
    - hundred  → 第几道刻痕（百位）
    - thousand → 第几个千阶
    - threshold→ 门槛分数
    - rank     → 越过了多少人
    只有"上升"才算刻痕：扣分不该出现"越过了 N 个人"。
    """
    events: List[Tuple[str, int]] = []

    if new_ladder > old_ladder:
        hundreds = crossed_hundreds(old_ladder, new_ladder)
        if hundreds > 0:
            events.append((KIND_HUNDRED, new_ladder // 100))

        thousands = crossed_thousands(old_ladder, new_ladder)
        if thousands > 0:
            events.append((KIND_THOUSAND, new_ladder // 1000))

        if crossed_threshold(old_ladder, new_ladder, min_ladder_score):
            events.append((KIND_THRESHOLD, min_ladder_score))

    if rank_before and rank_after and rank_after < rank_before:
        events.append((KIND_RANK, rank_before - rank_after))

    return events
