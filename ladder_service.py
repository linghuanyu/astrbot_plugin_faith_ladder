"""
Ladder service - core business logic for score management.
"""

from typing import Optional
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

        # Create player with specified scores
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

        return True, (
            f"玩家录入成功!\n"
            f"姓名: {player_name}\n"
            f"职业: {class_name} | 信仰: {faith_name}\n"
            f"天梯积分: {ladder_score} | 觐见之梯: {pilgrimage_score}"
        )
