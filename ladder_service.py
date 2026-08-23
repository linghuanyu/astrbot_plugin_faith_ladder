"""
Ladder service - core business logic for score management.
"""

import re
from typing import Optional, List, Dict, Any, Tuple, Callable, Awaitable
from astrbot_plugin_faith_ladder.models import Player, VALID_CLASSES, VALID_PATHS
from astrbot_plugin_faith_ladder.db_manager import DatabaseManager
from astrbot_plugin_faith_ladder.message_formatter import (
    format_leaderboard,
    format_pilgrimage_leaderboard,
    format_player_card,
    format_score_result,
    format_inventory,
)

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)


# === 道具等级解析 ===

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
    - 有效等级: '共生噬刃 |（C级）'
    - 非标准等级: '淬锋砺剑 |（无等级）'
    - 无等级: '铁剑'
    数量紧跟名字，等级在最后。

    ('共生噬刃', 'C', 3)  → '共生噬刃 * 3 |（C级）'
    ('共生噬刃', 'C', 1)  → '共生噬刃 |（C级）'
    ('淬锋砺剑', '', 3)   → '淬锋砺剑 * 3 |（无等级）'
    ('淬锋砺剑', '', 1)   → '淬锋砺剑 |（无等级）'
    ('铁剑', None, 5)     → '铁剑 * 5'
    ('铁剑', None, 1)     → '铁剑'
    """
    qty_str = f" * {quantity}" if quantity > 1 else ""
    if grade:
        return f"{item_name}{qty_str} |（{grade}级）"
    elif grade == "":
        return f"{item_name}{qty_str} |（无等级）"
    else:
        return f"{item_name}{qty_str}"


class LadderService:
    """Core business logic for the faith ladder plugin."""

    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager

    async def get_leaderboard_text(self, group_id: str, limit: int = 10) -> str:
        """Get formatted ladder leaderboard text."""
        players = await self.db.get_top_players(group_id, limit)
        return format_leaderboard(players, limit)

    async def get_pilgrimage_leaderboard_text(self, group_id: str, limit: int = 10) -> str:
        """Get formatted pilgrimage leaderboard text."""
        players = await self.db.get_top_players_by_pilgrimage(group_id, limit)
        return format_pilgrimage_leaderboard(players, limit)

    async def get_leaderboard_players(self, group_id: str, limit: int = 10) -> List[Player]:
        """Get top players for ladder leaderboard (for image rendering)."""
        return await self.db.get_top_players(group_id, limit)

    async def get_pilgrimage_leaderboard_players(self, group_id: str, limit: int = 10) -> List[Player]:
        """Get top players for pilgrimage leaderboard (for image rendering)."""
        return await self.db.get_top_players_by_pilgrimage(group_id, limit)

    async def get_effective_output_mode(self, group_id: str, global_default: str = "text") -> str:
        """Get effective output mode for a group.
        Checks DB for per-group override, falls back to global default.
        """
        db_mode = await self.db.get_group_output_mode(group_id)
        return db_mode if db_mode in ("text", "image") else global_default

    async def get_player_card_text(self, group_id: str, player_id: str) -> Optional[str]:
        """Get formatted player card text. Returns None if player not found."""
        player = await self.db.get_player(group_id, player_id)
        if not player:
            return None
        return format_player_card(player)

    async def get_player_card_by_name(
        self, group_id: str, player_name: str,
        init_ladder: int = 1000, init_pilgrimage: int = 100
    ) -> Optional[str]:
        """Get formatted player card by name. Returns None if not found."""
        player = await self.db.get_player_by_name(group_id, player_name)
        if not player:
            return None
        # 同分时按另一榜分数排序
        ladder_rank = await self.db.get_player_ladder_rank(
            group_id, player.ladder_score, player.pilgrimage_score
        )
        pilgrimage_rank = await self.db.get_player_pilgrimage_rank(
            group_id, player.pilgrimage_score, player.ladder_score
        )
        # 获取有效状态
        statuses = await self.db.get_player_statuses(group_id, player.player_id)
        return format_player_card(player, ladder_rank, pilgrimage_rank, init_ladder, init_pilgrimage, statuses)

    async def add_score(
        self,
        group_id: str,
        target_player_id: str,
        target_player_name: str,
        ladder_delta: int,
        pilgrimage_delta: int,
        operator_id: str,
        reason: str = "手动录入"
    ) -> tuple[bool, str]:
        """
        Add scores to a player. Player must already exist.
        Returns (success, message).
        """
        # Check player exists (do NOT auto-create)
        existing = await self.db.get_player(group_id, target_player_id)
        if not existing:
            return False, f"{target_player_name}不存在这个宇宙"

        # Update scores
        updated = await self.db.update_scores(
            group_id, target_player_id,
            ladder_delta, pilgrimage_delta,
            operator_id, reason
        )

        if not updated:
            return False, f"未找到玩家: {target_player_name}"

        return True, format_score_result(
            target_player_name,
            ladder_delta, pilgrimage_delta,
            updated.ladder_score, updated.pilgrimage_score
        )

    async def set_class(
        self,
        group_id: str,
        player_id: str,
        player_name: str,
        class_name: str,
    ) -> tuple[bool, str]:
        """
        Set a player's class only (faith is managed by 立誓 command).
        Returns (success, message).
        """
        # Validate class
        if not Player.validate_class(class_name):
            return False, f"无效职业: {class_name}。可选: {'/'.join(VALID_CLASSES)}"

        # Check player exists (do NOT auto-create)
        existing = await self.db.get_player(group_id, player_id)
        if not existing:
            return False, f"{player_name}不存在这个宇宙"

        # Set class only
        updated = await self.db.set_player_class(group_id, player_id, class_name, existing.faith)
        if not updated:
            return False, "设置失败，请重试。"

        return True, f"职业设置成功! 职业: {class_name}"

    async def set_faith(
        self,
        group_id: str,
        player_name: str,
        faith_name: str,
    ) -> tuple[bool, str]:
        """
        Set a player's faith. Does not clear oathbreaker status.
        Returns (success, message).
        """
        # Validate faith
        if not Player.validate_faith(faith_name):
            return False, f"无效命途: {faith_name}。可选: {'/'.join(VALID_PATHS)}"

        # Check player exists
        player = await self.db.get_player_by_name(group_id, player_name)
        if not player:
            return False, f"{player_name}不存在这个宇宙"

        updated = await self.db.set_player_faith(group_id, player.player_id, faith_name)
        if not updated:
            return False, "设置失败，请重试。"

        oathbreaker_tag = "（弃誓者）" if updated.oathbreaker else ""
        return True, f"立誓成功! {player_name}{oathbreaker_tag} 的信仰: {faith_name}"

    async def abandon_oath(
        self,
        group_id: str,
        player_name: str,
        new_faith: Optional[str],
        config: dict,
    ) -> tuple[bool, str]:
        """
        Mark a player as oathbreaker. Optionally set new faith.
        Returns (success, message).
        """
        # Check player exists
        player = await self.db.get_player_by_name(group_id, player_name)
        if not player:
            return False, f"{player_name}不存在这个宇宙"

        # Check player has a faith to abandon
        if not player.faith:
            return False, f"{player_name}尚无信仰，无誓可弃。"

        # Get oath text based on CURRENT faith
        current_faith = player.faith
        oath_text_key = f"oath_text_{current_faith}"
        oath_text = config.get(oath_text_key, f"{player_name}背弃了{current_faith}之道。誓约已碎。")
        oath_text = oath_text.replace("{name}", player_name)

        # Validate new faith if provided
        if new_faith and not Player.validate_faith(new_faith):
            return False, f"无效命途: {new_faith}。可选: {'/'.join(VALID_PATHS)}"

        # Set oathbreaker + optional faith change
        await self.db.set_oathbreaker(group_id, player.player_id, new_faith)

        result_parts = [oath_text]
        if new_faith:
            result_parts.append(f"\n{player_name} 的信仰已改为：{new_faith}（弃誓者）")
        else:
            result_parts.append(f"\n{player_name} 已被标记为弃誓者。")

        return True, "\n".join(result_parts)

    async def register_player(
        self,
        group_id: str,
        player_name: str,
        faith_name: str,
        class_name: str,
        ladder_score: int,
        pilgrimage_score: int,
        operator_id: str,
        qq_id: Optional[str] = None,
    ) -> tuple[bool, str]:
        """
        Register a new player with class, faith, and initial scores.
        Optionally bind a QQ ID to the new player (one QQ ↔ one player per group).
        Returns (success, message).
        All operations are committed atomically.
        """
        # Validate class
        if not Player.validate_class(class_name):
            return False, f"无效职业: {class_name}。可选: {'/'.join(VALID_CLASSES)}"

        # Validate faith
        if not Player.validate_faith(faith_name):
            return False, f"无效命途: {faith_name}。可选: {'/'.join(VALID_PATHS)}"

        # Check if player already exists
        existing = await self.db.get_player_by_name(group_id, player_name)
        if existing:
            return False, f"玩家 {player_name} 已存在，无法重复录入。"

        # Check QQ uniqueness up-front (friendly error before inserting)
        if qq_id:
            existing_qq = await self.db.get_player_by_qq(group_id, str(qq_id))
            if existing_qq:
                return False, (
                    f"QQ {qq_id} 已被玩家 {existing_qq.player_name} 绑定，"
                    "一个 QQ 在同一群只能绑定一个玩家。"
                )

        # Create player with specified scores (atomic: 3 operations in one transaction)
        player_id = f"name:{player_name}"
        await self.db.upsert_player(
            group_id, player_id, player_name,
            initial_ladder=ladder_score,
            initial_pilgrimage=pilgrimage_score,
        )

        # Set class and faith
        await self.db.set_player_class(group_id, player_id, class_name, faith_name)

        # Record in score history
        await self.db.update_scores(
            group_id, player_id, 0, 0,
            operator_id, f"录入玩家: {player_name}"
        )

        # Bind QQ if provided
        qq_binding_ok = False
        if qq_id:
            qq_binding_ok = await self.db.set_player_qq(group_id, player_id, str(qq_id))
            if not qq_binding_ok:
                # Race: another player bound this QQ between our check and now
                await self.db.rollback()
                return False, f"QQ {qq_id} 已被其他玩家绑定，注册回滚。"

        # Commit all operations atomically
        await self.db.commit()

        qq_line = f"\n绑定 QQ: {qq_id}" if qq_id and qq_binding_ok else ""
        return True, (
            f"=== 玩家信息 ===\n"
            f"姓名: {player_name}\n"
            f"职业: {class_name}\n"
            f"信仰: {faith_name}\n"
            f"登神之路: {ladder_score}\n"
            f"觐见之梯: {pilgrimage_score}"
            f"{qq_line}"
        )

    # === 批量录入 ===

    def parse_batch_scores(self, text: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """解析批量录入文本，提取玩家名、分数和道具。

        支持格式示例：
            【玩家：XXX 表现评分：A+】
            【登神之路+16】
            【觐见之梯+2】
            【获得道具：共生噬刃（C级）】
            【获得道具：生命药水】

        返回 (解析结果列表, 错误信息)。
        每个结果项: {"name": str, "ladder_delta": int, "pilgrimage_delta": int, "items": [str, ...]}
        """
        results = []
        # 按 "玩家：" 或 "玩家:" 分割，每段对应一个玩家的区块
        parts = re.split(r'玩家[：:]', text)

        for part in parts[1:]:  # 跳过第一段（"玩家："之前的内容）
            # 提取玩家名：
            # - 如果以 【 开头，提取到对应的 】 为止（支持带括号的名字如【吃鱼】）
            # - 否则取第一个空白/分隔符之前的内容
            stripped = part.lstrip()
            if stripped.startswith('【'):
                bracket_match = re.match(r'【(.+?)】', stripped)
                name = bracket_match.group(1) if bracket_match else None
            else:
                name_match = re.match(r'([^\s】，,：:]+)', stripped)
                name = name_match.group(1) if name_match else None

            if not name:
                continue

            # 提取天梯积分（兼容 "登神之路" / "封神之路" / "登神指路"，支持冒号和 +/-）
            ladder_match = re.search(r'(?:登神之路|封神之路|登神指路):?([+-])\s*(\d+)', part)
            if ladder_match:
                sign = 1 if ladder_match.group(1) == '+' else -1
                ladder_delta = sign * int(ladder_match.group(2))
            else:
                ladder_delta = 0

            # 提取觐见之梯分数（支持冒号和 +/-）
            pilgrimage_match = re.search(r'觐见之梯:?([+-])\s*(\d+)', part)
            if pilgrimage_match:
                sign = 1 if pilgrimage_match.group(1) == '+' else -1
                pilgrimage_delta = sign * int(pilgrimage_match.group(2))
            else:
                pilgrimage_delta = 0

            # 提取道具（【获得道具：名称】，支持空格分隔多个道具，支持 *数量 后缀）
            raw_items = re.findall(r'获得道具[：:]\s*([^】]+)', part)
            items = []
            for raw in raw_items:
                # 按空格分隔多个道具
                for item in raw.split():
                    item = item.strip()
                    if not item or item == "无":
                        continue
                    # 解析 *数量 后缀（如 美味糖果（C级）*3）
                    qty_match = re.match(r'^(.+)\*(\d+)$', item)
                    if qty_match:
                        name_part = qty_match.group(1).strip()
                        qty = int(qty_match.group(2))
                        if name_part and qty > 0:
                            # 添加 qty 次，让 Counter 正确统计
                            items.extend([name_part] * qty)
                    else:
                        items.append(item)

            if ladder_delta != 0 or pilgrimage_delta != 0 or items:
                results.append({
                    "name": name,
                    "ladder_delta": ladder_delta,
                    "pilgrimage_delta": pilgrimage_delta,
                    "items": items,
                })

        if not results:
            return [], "未从文本中解析到有效数据，请检查格式是否正确。"

        return results, None

    async def batch_add_scores(
        self,
        group_id: str,
        parsed_list: List[Dict[str, Any]],
        operator_id: str,
    ) -> Tuple[int, List[str], List[str]]:
        """批量录入积分和道具。所有更新在一个事务内完成。

        返回 (成功人数, 成功详情列表, 跳过玩家名列表)。
        """
        success_count = 0
        success_details = []
        skipped = []

        try:
            for entry in parsed_list:
                name = entry["name"]
                ladder_delta = entry["ladder_delta"]
                pilgrimage_delta = entry["pilgrimage_delta"]
                items = entry.get("items", [])

                # Check if player exists
                player = await self.db.get_player_by_name(group_id, name)
                if not player:
                    skipped.append(name)
                    continue

                # Update scores (without individual commit — deferred to end of batch)
                updated = await self.db.update_scores(
                    group_id, player.player_id,
                    ladder_delta, pilgrimage_delta,
                    operator_id, "批量录入",
                    commit=False
                )

                # Add items (count occurrences of each (base_name, grade) pair)
                item_details = []
                if items:
                    from collections import Counter
                    # 解析每个道具的基础名和等级，按 (base_name, grade) 分组计数
                    parsed_items = []
                    for raw in items:
                        base_name, grade = parse_item_full_name(raw)
                        parsed_items.append((base_name, grade))
                    item_counts = Counter(parsed_items)
                    for (base_name, grade), qty in item_counts.items():
                        await self.db.add_item(group_id, player.player_id, base_name, qty, grade=grade)
                        item_details.append(format_item_display(base_name, grade, qty))

                if updated or item_details:
                    success_count += 1
                    parts = []
                    if ladder_delta != 0 or pilgrimage_delta != 0:
                        ladder_str = f"+{ladder_delta}" if ladder_delta >= 0 else str(ladder_delta)
                        pilgrimage_str = f"+{pilgrimage_delta}" if pilgrimage_delta >= 0 else str(pilgrimage_delta)
                        parts.append(f"登神之路{ladder_str}, 觐见之梯{pilgrimage_str}")
                    if item_details:
                        parts.append(f"道具: {', '.join(item_details)}")
                    success_details.append(f"  {name}: {', '.join(parts)}")

            # Commit all updates atomically
            await self.db.commit()

        except Exception as e:
            logger.error(f"Batch update failed, rolling back: {e}")
            await self.db.rollback()
            return 0, [], [entry["name"] for entry in parsed_list]

        return success_count, success_details, skipped

    # === 储物空间 ===

    async def get_inventory_text(self, group_id: str, player_name: str) -> Optional[str]:
        """查询玩家储物空间。返回格式化文本，玩家不存在返回 None。"""
        player = await self.db.get_player_by_name(group_id, player_name)
        if not player:
            return None
        items = await self.db.get_player_items(group_id, player.player_id)
        return format_inventory(player_name, items)

    async def give_items(self, group_id: str, player_name: str, items: List[Tuple[str, int]]) -> Tuple[bool, str]:
        """赐予道具。items: [(道具名（可能含等级）, 数量), ...]"""
        player = await self.db.get_player_by_name(group_id, player_name)
        if not player:
            return False, f"玩家 {player_name} 不存在"
        details = []
        for raw_name, quantity in items:
            base_name, grade = parse_item_full_name(raw_name)
            await self.db.add_item(group_id, player.player_id, base_name, quantity, grade=grade)
            details.append(format_item_display(base_name, grade, quantity))
        await self.db.commit()
        return True, f"已赐予 {player_name}: {', '.join(details)}"

    async def take_items(self, group_id: str, player_name: str, items: List[Tuple[str, Optional[int]]]) -> Tuple[bool, str]:
        """收回道具。items: [(道具名（可能含等级）, 数量或None), ...]。None=全部收回。按 item_name 匹配，不需要等级。"""
        player = await self.db.get_player_by_name(group_id, player_name)
        if not player:
            return False, f"玩家 {player_name} 不存在"
        success_details = []
        fail_details = []
        for raw_name, quantity in items:
            base_name, grade = parse_item_full_name(raw_name)
            # 按 item_name 匹配（不要求等级一致），收回实际道具
            found_items = await self.db.get_player_items(group_id, player.player_id)
            match = next((i for i in found_items if i["item_name"] == base_name), None)
            if not match:
                fail_details.append(f"收回失败：{player_name} 没有道具 {base_name}")
                continue
            actual_grade = match["grade"]
            actual_qty = match["quantity"]
            await self.db.remove_item(group_id, player.player_id, base_name, quantity, grade=actual_grade)
            if quantity is None:
                # 全部收回，显示实际收回数量
                success_details.append(format_item_display(base_name, actual_grade, actual_qty))
            else:
                success_details.append(format_item_display(base_name, actual_grade, quantity))
        await self.db.commit()

        if not success_details:
            # 全部失败，只返回失败信息
            return False, "\n".join(fail_details)
        elif not fail_details:
            # 全部成功
            return True, f"已从 {player_name} 收回: {', '.join(success_details)}"
        else:
            # 部分成功部分失败
            return True, f"已从 {player_name} 收回: {', '.join(success_details)}\n" + "\n".join(fail_details)

    # === 状态 ===

    async def add_status(self, group_id: str, player_name: str, status_name: str, days: int) -> Tuple[bool, str]:
        """添加状态。"""
        player = await self.db.get_player_by_name(group_id, player_name)
        if not player:
            return False, f"玩家 {player_name} 不存在"
        await self.db.add_status(group_id, player.player_id, status_name, days)
        await self.db.commit()
        return True, f"已为 {player_name} 添加状态 [{status_name}]（持续{days}天）"

    async def remove_status(self, group_id: str, player_name: str, status_name: str) -> Tuple[bool, str]:
        """移除指定状态。"""
        player = await self.db.get_player_by_name(group_id, player_name)
        if not player:
            return False, f"玩家 {player_name} 不存在"
        found = await self.db.remove_status(group_id, player.player_id, status_name)
        await self.db.commit()
        if found:
            return True, f"已移除 {player_name} 的状态 [{status_name}]"
        return False, f"{player_name} 没有状态 [{status_name}]"

    async def clear_statuses(self, group_id: str, player_name: str) -> Tuple[bool, str]:
        """清除所有状态。"""
        player = await self.db.get_player_by_name(group_id, player_name)
        if not player:
            return False, f"玩家 {player_name} 不存在"
        count = await self.db.clear_statuses(group_id, player.player_id)
        await self.db.commit()
        return True, f"已清除 {player_name} 的 {count} 个状态"

    # === 赠送道具 ===

    async def deduct_item(
        self, group_id: str, player_id: str, player_name: str,
        raw_item_name: str, quantity: int
    ) -> Tuple[bool, str, Optional[str], Optional[str]]:
        """扣除道具。返回 (success, msg, base_name, grade)。
        输入 raw_item_name 可含等级，如 '共生噬刃（C级）'。
        无等级输入时按 item_name 匹配第一个。"""
        base_name, input_grade = parse_item_full_name(raw_item_name)
        items = await self.db.get_player_items(group_id, player_id)
        if input_grade:
            match = next((i for i in items if i["item_name"] == base_name and i["grade"] == input_grade), None)
        else:
            match = next((i for i in items if i["item_name"] == base_name), None)
        if not match:
            return False, f"没有道具: {base_name}", base_name, input_grade
        if match["quantity"] < quantity:
            return False, f"道具不足：你只有 {match['quantity']} 个 {format_item_display(base_name, match['grade'], match['quantity'])}", base_name, input_grade
        await self.db.remove_item(group_id, player_id, base_name, quantity, grade=match["grade"])
        await self.db.commit()
        return True, f"已扣除 {format_item_display(base_name, match['grade'], quantity)}", base_name, match["grade"]

    async def receive_item(
        self, group_id: str, player_id: str, player_name: str,
        item_name: str, quantity: int, grade: Optional[str] = None
    ) -> Tuple[bool, str]:
        """接收道具（接收方接受时调用）。"""
        await self.db.add_item(group_id, player_id, item_name, quantity, grade=grade)
        await self.db.commit()
        return True, f"已收到 {format_item_display(item_name, grade, quantity)}"

    async def cleanup_expired_gifts(
        self, max_age_seconds: int = 240,
        notify: Optional[Callable[[str, str], Awaitable[None]]] = None,
    ) -> int:
        """清理超时的待处理赠送，退回道具给发送方。返回退回数量。
        notify(group_id, text) 用于向原群发送超时通知，可选。"""
        expired = await self.db.get_expired_pending_gifts(max_age_seconds)
        refunded = 0
        for gift in expired:
            try:
                items = gift["items"]
                await self.receive_item(
                    gift["group_id"], gift["sender_id"], gift["sender_name"],
                    items["item_name"], items["quantity"], grade=items.get("grade")
                )
                await self.db.delete_pending_gift(gift["group_id"], gift["receiver_id"])
                refunded += 1
                display = format_item_display(items["item_name"], items.get("grade"), items["quantity"])
                logger.info(
                    f"[GiftCleanup] 超时退回：{gift['sender_name']} -> {gift['receiver_name']} "
                    f"({display})"
                )
                if notify:
                    try:
                        await notify(
                            gift["group_id"],
                            f"⏰ 赠送超时自动退回：{gift['sender_name']} → {gift['receiver_name']} 的 {display} 已退回发送方"
                        )
                    except Exception as e:
                        logger.error(f"[GiftCleanup] 通知发送失败: {e}")
            except Exception as e:
                logger.error(f"[GiftCleanup] 退回失败（{gift['sender_name']} -> {gift['receiver_name']}）: {e}")
        return refunded

    async def clear_items(self, group_id: str, player_name: str, raw_name: Optional[str] = None) -> Tuple[bool, str]:
        """清除储物空间。raw_name=None → 清空全部；指定道具名 → 清除该道具（可含等级）。"""
        player = await self.db.get_player_by_name(group_id, player_name)
        if not player:
            return False, f"玩家 {player_name} 不存在"
        if raw_name is None:
            count = await self.db.clear_items(group_id, player.player_id)
            return True, f"已清空 {player_name} 的储物空间（{count} 种道具）"
        else:
            base_name, grade = parse_item_full_name(raw_name)
            count = await self.db.clear_items(group_id, player.player_id, base_name, grade)
            if count == 0:
                return False, f"{player_name} 没有此道具"
            display = format_item_display(base_name, grade, 1)
            return True, f"已清除 {player_name} 的 {display}"
