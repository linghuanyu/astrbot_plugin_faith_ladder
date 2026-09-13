"""
道具名解析与展示格式化（纯工具函数，无外部依赖）。
"""

import re
from typing import List, Optional, Tuple

VALID_GRADES = ("SSS", "SS", "S", "A", "B", "C")

# 匹配：名字 + 括号 + 内容（提取字母） + 括号
# 支持格式：（C级）、(B)、（b+级）、（A残级）、（枯纹根茎）等
# 从括号内容中提取开头的字母作为等级，忽略中文和其他字符
_GRADE_RE = re.compile(r'^(.*)[（(]([^)）]*)[)）]$')

# 数量标记：*数字 或 ×数字（× 是本插件展示格式用的乘号，便于直接复制粘贴）
_QTY_RE = re.compile(r'[*×](\d+)')

# ── 等级在「解析侧」与「存储侧」之间的映射 ──
#
# 解析侧（parse_item_full_name）是三态：
#   None → 名字里根本没有等级括号，如 "铁剑"
#   ""   → 有括号但不是有效等级，如 "淬锋砺剑（D）"
#   "C"  → 有效等级
#
# 存储侧要求 NOT NULL：player_items 的主键包含 grade，而 SQLite 中 NULL 彼此不相等
# （UNIQUE 约束对 NULL 不生效），若用 NULL 表示"无等级"，同名无等级道具会被重复
# 插入而不是合并进同一行。因此把三态压缩成两个哨兵字符串。
GRADE_STORAGE_NONE = ""           # 无等级括号
GRADE_STORAGE_NONSTANDARD = "-"   # 有括号但非标准等级


def grade_to_storage(grade: Optional[str]) -> str:
    """解析侧的三态等级 → 数据库存储值（NOT NULL）。None → ""；"" → "-"。"""
    if grade is None:
        return GRADE_STORAGE_NONE
    if grade == "":
        return GRADE_STORAGE_NONSTANDARD
    return grade


def grade_from_storage(stored: Optional[str]) -> Optional[str]:
    """数据库存储值 → 解析侧的三态等级，供展示与比较使用。

    读出的等级统一回到 None / "" / "C" 三态，调用方无需关心存储哨兵写法。
    """
    if stored is None or stored == GRADE_STORAGE_NONE:
        return None
    if stored == GRADE_STORAGE_NONSTANDARD:
        return ""
    return stored


def extract_item_quantity(text: str) -> Tuple[str, Optional[int]]:
    """从单个道具标记中分离数量，返回 (去掉数量后的标记, 数量)。

    数量写作「*数字」或「×数字」，**位置不限**，等级括号之前或之后都能识别。
    没有数量标记时第二个返回值为 None（由调用方决定默认值：多为 1，
    「收回道具」中表示全部收回）。

    '测试*10（b）'      → ('测试（b）', 10)
    '测试（b）*10'      → ('测试（b）', 10)
    '测试×10（b）'      → ('测试（b）', 10)
    '共生噬刃×3（C级）' → ('共生噬刃（C级）', 3)
    '测试*10'           → ('测试', 10)
    '测试（b）'          → ('测试（b）', None)
    '铁剑'              → ('铁剑', None)
    """
    m = _QTY_RE.search(text)
    if not m:
        return text.strip(), None
    rest = (text[:m.start()] + text[m.end():]).strip()
    return rest, int(m.group(1))


def parse_item_args(text: str) -> List[Tuple[str, int]]:
    """解析用户输入的道具参数，返回 [(道具名（可能含等级）, 数量), ...]。

    空格分隔多个道具，数量有两种写法且可混用：
    - 紧跟名字（空格分隔）: '铁剑 2 生命药水 3'、'共生噬刃（C级） 2'
    - 用 * 或 × 标注，位置不限: '铁剑*2'、'测试（b）*10'、'测试*10（b）'

    数量必须为正整数，否则抛 ValueError —— 负数量在 SQL 里会让"扣除"变成"增加"，
    等于凭空造道具，必须在入口拦住。

    放在本模块（而非 main.py）是为了让它可测试：main.py 依赖 astrbot，
    测试环境无法导入。
    """
    items: List[Tuple[str, int]] = []
    parts = text.strip().split()
    i = 0
    while i < len(parts):
        name, qty = extract_item_quantity(parts[i])
        if qty is not None:
            if not name:
                raise ValueError("道具名不能为空")
            if qty <= 0:
                raise ValueError(f"「{name}」的数量必须为正整数")
            items.append((name, qty))
            i += 1
            continue
        # 没有 *数量 标记：看下一个 token 是否是数字（数量）
        if i + 1 < len(parts):
            try:
                next_qty = int(parts[i + 1])
            except ValueError:
                pass
            else:
                if next_qty <= 0:
                    raise ValueError(f"「{parts[i]}」的数量必须为正整数")
                items.append((parts[i], next_qty))
                i += 2
                continue
        # 下一个不是数字，当前作为独立道具（数量 1）
        items.append((parts[i], 1))
        i += 1
    return items


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
