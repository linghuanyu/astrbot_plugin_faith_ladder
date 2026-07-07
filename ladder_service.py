"""
Ladder service - core business logic for score management.
"""

import re
from typing import Optional, List, Dict, Any, Tuple
from astrbot_plugin_faith_ladder.models import Player, VALID_CLASSES, VALID_FAITHS
from astrbot_plugin_faith_ladder.db_manager import DatabaseManager
from astrbot_plugin_faith_ladder.message_formatter import (
    format_leaderboard,
    format_pilgrimage_leaderboard,
    format_player_card,
    format_score_result,
)


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

    async def get_player_card_by_name(self, group_id: str, player_name: str) -> Optional[str]:
        """Get formatted player card by name. Returns None if not found."""
        player = await self.db.get_player_by_name(group_id, player_name)
        if not player:
            return None
        return format_player_card(player)

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
        Add scores to a player.
        Returns (success, message).
        """
        # Ensure player exists
        await self.db.upsert_player(group_id, target_player_id, target_player_name)

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
        faith_name: str
    ) -> tuple[bool, str]:
        """
        Set a player's class and faith.
        Returns (success, message).
        """
        # Validate class
        if not Player.validate_class(class_name):
            return False, f"无效职业: {class_name}。可选: {'/'.join(VALID_CLASSES)}"

        # Validate faith
        if not Player.validate_faith(faith_name):
            return False, f"无效信仰: {faith_name}。可选: {'/'.join(VALID_FAITHS)}"

        # Ensure player exists
        await self.db.upsert_player(group_id, player_id, player_name)

        # Set class and faith
        updated = await self.db.set_player_class(group_id, player_id, class_name, faith_name)
        if not updated:
            return False, "设置失败，请重试。"

        return True, f"职业设置成功! 职业: {class_name}, 信仰: {faith_name}"

    async def register_player(
        self,
        group_id: str,
        player_name: str,
        faith_name: str,
        class_name: str,
        ladder_score: int,
        pilgrimage_score: int,
        operator_id: str,
    ) -> tuple[bool, str]:
        """
        Register a new player with class, faith, and initial scores.
        Returns (success, message).
        All 3 DB operations are committed atomically.
        """
        # Validate class
        if not Player.validate_class(class_name):
            return False, f"无效职业: {class_name}。可选: {'/'.join(VALID_CLASSES)}"

        # Validate faith
        if not Player.validate_faith(faith_name):
            return False, f"无效信仰: {faith_name}。可选: {'/'.join(VALID_FAITHS)}"

        # Check if player already exists
        existing = await self.db.get_player_by_name(group_id, player_name)
        if existing:
            return False, f"玩家 {player_name} 已存在，无法重复录入。"

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

        # Commit all 3 operations atomically
        await self.db.commit()

        return True, (
            f"玩家录入成功!\n"
            f"姓名: {player_name}\n"
            f"职业: {class_name} | 信仰: {faith_name}\n"
            f"天梯积分: {ladder_score} | 觐见之梯: {pilgrimage_score}"
        )

    # === 批量录入 ===

    def parse_batch_scores(self, text: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """解析批量录入文本，提取玩家名和分数。

        支持格式示例：
            【玩家：XXX 表现评分：A+】
            【登神之路+16】
            【觐见之梯+2】

        返回 (解析结果列表, 错误信息)。
        每个结果项: {"name": str, "ladder_delta": int, "pilgrimage_delta": int}
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

            # 提取天梯积分（兼容 "登神之路" / "登神指路"，支持 +/-）
            ladder_match = re.search(r'登神[之指]路([+-])\s*(\d+)', part)
            if ladder_match:
                sign = 1 if ladder_match.group(1) == '+' else -1
                ladder_delta = sign * int(ladder_match.group(2))
            else:
                ladder_delta = 0

            # 提取觐见之梯分数（支持 +/-）
            pilgrimage_match = re.search(r'觐见之梯([+-])\s*(\d+)', part)
            if pilgrimage_match:
                sign = 1 if pilgrimage_match.group(1) == '+' else -1
                pilgrimage_delta = sign * int(pilgrimage_match.group(2))
            else:
                pilgrimage_delta = 0

            if ladder_delta != 0 or pilgrimage_delta != 0:
                results.append({
                    "name": name,
                    "ladder_delta": ladder_delta,
                    "pilgrimage_delta": pilgrimage_delta,
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
        """批量录入积分。

        返回 (成功人数, 成功详情列表, 跳过玩家名列表)。
        """
        success_count = 0
        success_details = []
        skipped = []

        for entry in parsed_list:
            name = entry["name"]
            ladder_delta = entry["ladder_delta"]
            pilgrimage_delta = entry["pilgrimage_delta"]

            # Check if player exists
            player = await self.db.get_player_by_name(group_id, name)
            if not player:
                skipped.append(name)
                continue

            # Update scores
            updated = await self.db.update_scores(
                group_id, player.player_id,
                ladder_delta, pilgrimage_delta,
                operator_id, "批量录入"
            )
            if updated:
                success_count += 1
                ladder_str = f"+{ladder_delta}" if ladder_delta >= 0 else str(ladder_delta)
                pilgrimage_str = f"+{pilgrimage_delta}" if pilgrimage_delta >= 0 else str(pilgrimage_delta)
                success_details.append(
                    f"  {name}: 天梯积分{ladder_str}, 觐见之梯{pilgrimage_str}"
                )

        return success_count, success_details, skipped
