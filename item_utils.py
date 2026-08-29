"""
道具名解析与展示格式化（纯工具函数，无外部依赖）。
"""

import re
from typing import Optional, Tuple

VALID_GRADES = ("SSS", "SS", "S", "A", "B", "C")

# 匹配：名字 + 括号 + 内容（提取字母） + 括号
# 支持格式：（C级）、(B)、（b+级）、（A残级）、（枯纹根茎）等
# 从括号内容中提取开头的字母作为等级，忽略中文和其他字符
_GRADE_RE = re.compile(r'^(.*)[（(]([^)）]*)[)）]$')


def parse_item_full_name(full_name: str) -> Tuple[str, Optional[str]]:
    """从完整名解析出 (基础名, 等级)。
    等级中的 + 号会被自动去除（如 B+ → B）。
    括号内有字母则提取字母作为等级，忽略中文和其他字符。
    返回三种状态:
    - grade 为有效等级字符串（SSS/SS/S/A/B/C）→ 有效等级
    - grade 为 ""（空字符串）→ 有括号但不是有效等级（如 D/d/中文）
    - grade 为 None → 完全没有等级括号

    '共生噬刃（C级）'    → ('共生噬刃', 'C')
    '共生噬刃(C)'        → ('共生噬刃', 'C')
    '道具名（b+级）'     → ('道具名', 'B')     — + 号去除后 B 有效
    '共识之杖（A残级）'  → ('共识之杖', 'A')   — "残"被忽略，提取 A
    '(B）'              → ('', 'B')           — 空基础名，提取 B
    '淬锋砺剑（D）'      → ('淬锋砺剑', '')    — D 不在 VALID_GRADES
    '塑形内衣（d级）'    → ('塑形内衣', '')    — d→D 不在 VALID_GRADES
    '繁荣新芽（枯纹根茎）' → ('繁荣新芽', '')  — 无字母，空字符串
    '铁剑'              → ('铁剑', None)
    """
    m = _GRADE_RE.match(full_name.strip())
    if m:
        base = m.group(1).strip()
        content = m.group(2).strip()
        # 从括号内容中提取开头的字母（支持 + 号）
        grade_match = re.match(r'^([A-Za-z]+\+?)', content)
        if grade_match:
            grade = grade_match.group(1).upper().rstrip('+')
            if grade in VALID_GRADES:
                return base, grade
            # 有字母但不是有效等级 → 返回空字符串标记
            return base, ""
        # 括号内无字母（如中文）→ 返回空字符串标记
        return base, ""
    return full_name.strip(), None


def format_item_display(item_name: str, grade: Optional[str], quantity: int) -> str:
    """格式化道具展示。数量1不显*1。
    三种格式:
    - 有效等级: '共生噬刃×3（C级）'
    - 非标准等级: '淬锋砺剑×3（无等级）'
    - 无等级: '铁剑×5'
    数量紧跟名字，等级在最后。

    ('共生噬刃', 'C', 3)  → '共生噬刃×3（C级）'
    ('共生噬刃', 'C', 1)  → '共生噬刃（C级）'
    ('淬锋砺剑', '', 3)   → '淬锋砺剑×3（无等级）'
    ('淬锋砺剑', '', 1)   → '淬锋砺剑（无等级）'
    ('铁剑', None, 5)     → '铁剑×5'
    ('铁剑', None, 1)     → '铁剑'
    """
    qty_str = f"×{quantity}" if quantity > 1 else ""
    if grade:
        return f"{item_name}{qty_str}（{grade}级）"
    elif grade == "":
        return f"{item_name}{qty_str}（无等级）"
    else:
        return f"{item_name}{qty_str}"
