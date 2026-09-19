"""
Ladder service - core business logic for score management.
"""

import re
import time
from typing import Optional, List, Dict, Any, Tuple, Callable, Awaitable
from astrbot_plugin_faith_ladder.models import Player, VALID_CLASSES, VALID_PATHS, FAITH_TO_PATH
from astrbot_plugin_faith_ladder.db_manager import DatabaseManager
from astrbot_plugin_faith_ladder.message_formatter import (
    format_leaderboard,
    format_pilgrimage_leaderboard,
    format_player_card,
    format_score_result,
    format_inventory,
    format_faith_line,
)
from astrbot_plugin_faith_ladder.plugin_config import cfg_get
from astrbot_plugin_faith_ladder.item_utils import (
    parse_item_full_name,
    format_item_display,
    extract_item_quantity,
)

try:
    from astrbot.api import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)

# 单个道具一次批量录入的数量上限。解析时按数量展开成列表，不设上限的话
# 一条 `铁剑*2000000` 就能让插件吃掉几十 MB 内存。
MAX_ITEM_QUANTITY = 9999


class LadderService:
    """Core business logic for the faith ladder plugin."""

    # 排行榜缓存 TTL（秒）
    LEADERBOARD_CACHE_TTL = 30  # 30 秒

    def __init__(self, db_manager: DatabaseManager):
        """装配业务逻辑层：持有 DB 管理器与排行榜缓存（缓存 TTL 见 LEADERBOARD_CACHE_TTL）。"""
        self.db = db_manager
        # 排行榜缓存：{(group_id, limit): (players, timestamp)}
        self._leaderboard_cache = {}
        self._pilgrimage_cache = {}

    def invalidate_leaderboard_cache(self, group_id: str = None):
        """失效排行榜缓存。不传 group_id 则清空全部缓存。"""
        if group_id:
            keys_to_remove = [k for k in self._leaderboard_cache if k[0] == group_id]
            for k in keys_to_remove:
                del self._leaderboard_cache[k]
            keys_to_remove = [k for k in self._pilgrimage_cache if k[0] == group_id]
            for k in keys_to_remove:
                del self._pilgrimage_cache[k]
        else:
            self._leaderboard_cache.clear()
            self._pilgrimage_cache.clear()

    async def get_leaderboard_text(self, group_id: str, limit: int = 10, min_ladder_score: int = 0) -> str:
        """Get formatted ladder leaderboard text.

        min_ladder_score 为榜单门槛：低于该分不上榜。榜空且门槛 > 0 时说明原因，
        否则玩家会以为是数据丢了。
        """
        players = await self.get_top_players(group_id, limit, min_ladder_score)
        if not players and min_ladder_score > 0:
            return f"暂无排名数据（登神之路低于 {min_ladder_score} 分不上榜）。"
        return format_leaderboard(players, limit)

    async def get_top_players(self, group_id: str, limit: int = 10, min_ladder_score: int = 0) -> List[Player]:
        """获取登神之路排行榜，带 30 秒缓存。

        缓存键必须带上门槛：门槛来自配置，WebUI 改完后若只按 (群, 人数) 命中旧缓存，
        会继续按旧门槛显示最多 30 秒。
        """
        cache_key = (group_id, limit, min_ladder_score)
        now = time.time()
        if cache_key in self._leaderboard_cache:
            players, timestamp = self._leaderboard_cache[cache_key]
            if now - timestamp < self.LEADERBOARD_CACHE_TTL:
                return players
        players = await self.db.get_top_players(group_id, limit, min_ladder_score)
        self._leaderboard_cache[cache_key] = (players, now)
        return players

    async def get_pilgrimage_leaderboard_text(self, group_id: str, limit: int = 10) -> str:
        """Get formatted pilgrimage leaderboard text."""
        players = await self.get_top_players_by_pilgrimage(group_id, limit)
        return format_pilgrimage_leaderboard(players, limit)

    async def get_top_players_by_pilgrimage(self, group_id: str, limit: int = 10) -> List[Player]:
        """获取觐见之梯排行榜，带 30 秒缓存。"""
        cache_key = (group_id, limit)
        now = time.time()
        if cache_key in self._pilgrimage_cache:
            players, timestamp = self._pilgrimage_cache[cache_key]
            if now - timestamp < self.LEADERBOARD_CACHE_TTL:
                return players
        players = await self.db.get_top_players_by_pilgrimage(group_id, limit)
        self._pilgrimage_cache[cache_key] = (players, now)
        return players

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


    @staticmethod
    def _ranks_by(sorted_players: List[Player], key) -> Dict[str, int]:
        """按「严格领先的人数 + 1」计算名次：成绩完全相同者并列同一名次。

        与 DB 侧 get_player_ladder_rank / get_player_pilgrimage_rank 的
        COUNT(*) 口径保持一致。批量查询此前用 enumerate 直接给序号，
        并列时会排出不同名次，导致同一个玩家在单查与批量查里名次不同。
        sorted_players 必须已按同一 key 降序排好。
        """
        ranks: Dict[str, int] = {}
        prev_key = None
        prev_rank = 0
        for i, player in enumerate(sorted_players):
            cur_key = key(player)
            if prev_key is not None and cur_key == prev_key:
                ranks[player.player_id] = prev_rank
            else:
                prev_rank = i + 1
                ranks[player.player_id] = prev_rank
            prev_key = cur_key
        return ranks

    async def get_player_cards_by_names(
        self, group_id: str, player_names: List[str],
        init_ladder: int = 1000, init_pilgrimage: int = 100
    ) -> tuple:
        """批量查询玩家信息。返回 (cards_text, not_found_names)。
        优化：一次性获取全组玩家计算排名 + 批量查状态，将 3N+1 次 DB 降为 3 次。
        """
        if not player_names:
            return "", []

        # 1. 批量查询玩家（1 次 SQL）
        # 先按顺序去重：重复的名字会产出重复的卡片与重复的"不存在"提示
        seen = set()
        unique_names = [n for n in player_names if not (n in seen or seen.add(n))]
        players_dict = await self.db.get_players_by_names(group_id, unique_names)
        not_found = [name for name in unique_names if name not in players_dict]

        if not players_dict:
            return "", not_found

        # 按原始顺序获取玩家列表
        players = [players_dict[name] for name in unique_names if name in players_dict]

        # 2. 一次性获取全组玩家，客户端派生排名（1 次 SQL）
        all_players = await self.db.get_all_players_in_group(group_id)
        ladder_sorted = sorted(all_players, key=lambda p: (p.ladder_score, p.pilgrimage_score), reverse=True)
        pilgrimage_sorted = sorted(all_players, key=lambda p: (p.pilgrimage_score, p.ladder_score), reverse=True)
        ladder_ranks = self._ranks_by(ladder_sorted, lambda p: (p.ladder_score, p.pilgrimage_score))
        pilgrimage_ranks = self._ranks_by(pilgrimage_sorted, lambda p: (p.pilgrimage_score, p.ladder_score))

        # 3. 批量查状态（1 次 SQL）
        player_ids = [p.player_id for p in players]
        all_statuses = await self.db.get_statuses_for_players(group_id, player_ids)
        status_map = {pid: stats for pid, stats in all_statuses}

        # 组装卡片
        cards = []
        for player in players:
            ladder_rank = ladder_ranks.get(player.player_id, 0)
            pilgrimage_rank = pilgrimage_ranks.get(player.player_id, 0)
            statuses = status_map.get(player.player_id, [])
            card = format_player_card(player, ladder_rank, pilgrimage_rank, init_ladder, init_pilgrimage, statuses)
            cards.append(card)

        # 合并所有玩家信息
        combined = "\n\n".join(cards)
        return combined, not_found


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

        # 失效排行榜缓存（积分变化后排行榜可能变化）
        self.invalidate_leaderboard_cache(group_id)

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

        # 榜单会显示职业，改了必须失效缓存
        self.invalidate_leaderboard_cache(group_id)
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
        # 命途换了之后，原具体信仰可能已不属于新命途，对账清掉
        await self._reconcile_specific_faith(group_id, player.player_id, faith_name)

        # 榜单会显示信仰，改了必须失效缓存
        self.invalidate_leaderboard_cache(group_id)
        oathbreaker_tag = "（弃誓者）" if updated.oathbreaker else ""
        return True, f"立誓成功! {player_name}{oathbreaker_tag} 的信仰: {faith_name}"

    async def _reconcile_specific_faith(
        self, group_id: str, player_id: str, path: Optional[str]
    ) -> None:
        """让具体信仰与命途保持一致。

        命途被清空（弃誓不留新信仰）或与具体信仰所属命途不符时，清掉具体信仰。
        否则会出现「信仰：None | 繁荣」「信仰：文明 | 繁荣」这类自相矛盾的展示，
        按具体信仰取主题文案时键也会对不上。
        """
        player = await self.db.get_player(group_id, player_id)
        if not player or not player.specific_faith:
            return
        if path and FAITH_TO_PATH.get(player.specific_faith) == path:
            return
        await self.db.set_player_specific_faith(group_id, player_id, None)
        logger.info(
            f"[Faith] 具体信仰与命途不一致，已清除: {player.player_name} "
            f"specific={player.specific_faith} path={path}"
        )

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

        # 弃誓文案按「具体信仰」取值：_conf_schema.json 里的 oath_text_* 是 16 个具体信仰
        # （oath_text_诞育 … oath_text_命运），而 player.faith 存的是 6 命途，
        # 之前直接拼 oath_text_{命途} 导致 16 个配置键全部取不到、永远用兜底文案。
        # 没有具体信仰时（名片里只有命途）才回退到命途键，最后再兜底。
        current_faith = player.faith
        specific_faith = player.specific_faith
        oath_text = None
        if specific_faith:
            oath_text = cfg_get(config, f"oath_text_{specific_faith}")
        if not oath_text:
            oath_text = cfg_get(config, f"oath_text_{current_faith}")
        if not oath_text:
            oath_text = f"{player_name}背弃了{specific_faith or current_faith}之道。誓约已碎。"
        oath_text = oath_text.replace("{name}", player_name)

        # Validate new faith if provided
        if new_faith and not Player.validate_faith(new_faith):
            return False, f"无效命途: {new_faith}。可选: {'/'.join(VALID_PATHS)}"

        # Set oathbreaker + optional faith change
        await self.db.set_oathbreaker(group_id, player.player_id, new_faith)
        # 弃誓未指定新命途时命途会被清空，此时具体信仰也必须一起清，
        # 否则玩家档案会显示成「信仰：None | 繁荣」
        await self._reconcile_specific_faith(group_id, player.player_id, new_faith)

        # 榜单会显示"弃誓者"标记 → 失效缓存
        self.invalidate_leaderboard_cache(group_id)

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
        specific_faith: Optional[str] = None,
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

        # 以下四步共用同一个事务（commit=False），QQ 绑定失败时 rollback 才真正能撤销，
        # 否则子操作各自提交，"注册回滚"只是句谎话（玩家/职业/历史记录其实已经落库）
        player_id = f"name:{player_name}"
        await self.db.upsert_player(
            group_id, player_id, player_name,
            initial_ladder=ladder_score,
            initial_pilgrimage=pilgrimage_score,
            commit=False,
        )

        # Set class and faith
        await self.db.set_player_class(group_id, player_id, class_name, faith_name, commit=False)

        # 具体信仰（如"繁荣"）：名片里解析到就一并落库，查询/文案/主题消息都用它；
        # 放在同一事务内，失败时随其它步骤一起回滚
        if specific_faith:
            await self.db.set_player_specific_faith(
                group_id, player_id, specific_faith, commit=False
            )

        # Record in score history
        await self.db.update_scores(
            group_id, player_id, 0, 0,
            operator_id, f"录入玩家: {player_name}",
            commit=False,
        )

        # Bind QQ if provided
        if qq_id:
            qq_binding_ok = await self.db.set_player_qq(group_id, player_id, str(qq_id), commit=False)
            if not qq_binding_ok:
                # Race: another player bound this QQ between our check and now
                await self.db.rollback()
                return False, f"QQ {qq_id} 已被其他玩家绑定，注册回滚。"

        # Commit all operations atomically
        await self.db.commit()
        # 新玩家会影响榜单名次，必须失效缓存（否则榜单最多陈旧 30 秒）
        self.invalidate_leaderboard_cache(group_id)

        # 尝试使用信仰专属文案
        try:
            from astrbot_plugin_faith_ladder.faith_messages import FAITH_MESSAGES, GENERIC_GOD_MESSAGES
            import random
            # 按具体信仰取文案；FAITH_MESSAGES 的键是 16 个具体信仰，
            # 而 faith_name 是 6 个命途之一，直接用它查永远查不到，只能退回通用文案
            faith_key = specific_faith or faith_name
            faith_messages = FAITH_MESSAGES.get(faith_key, {}).get("register_success", [])
            if not faith_messages:
                faith_messages = GENERIC_GOD_MESSAGES.get("register_success", [])
            if faith_messages:
                flavor_text = random.choice(faith_messages)
                return True, (
                    f"「{player_name}」踏入信仰之途\n"
                    f"职业: {class_name} \n"
                    f" {format_faith_line(faith_name, specific_faith)}\n"
                    f"登神之路: {ladder_score}\n"
                    f"觐见之梯: {pilgrimage_score}\n"
                    f"{flavor_text}"
                )
        except Exception:
            pass

        # 回退到默认文案
        ladder_tag = "凡人之始" if ladder_score == 1000 else ""
        pilgrimage_tag = "初窥门径" if pilgrimage_score == 100 else ""
        tags = []
        if ladder_tag:
            tags.append(f"—— {ladder_tag}")
        if pilgrimage_tag:
            tags.append(f"—— {pilgrimage_tag}")

        return True, (
            f"「{player_name}」踏入信仰之途\n"
            f"职业: {class_name} \n"
            f" {format_faith_line(faith_name, specific_faith)}\n"
            f"登神之路: {ladder_score} {' '.join(tags[:1])}\n"
            f"觐见之梯: {pilgrimage_score} {' '.join(tags[1:])}\n"
            f"愿神明不要愚弄你。"
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

            # 提取天梯积分（兼容 "登神之路" / "封神之路" / "登神指路"）
            # 正负号与冒号都是可选的：文档一直这么承诺，但正则强制要求 [+-]，
            # 于是「【登神之路 16】」被静默当成 0（若这是该玩家唯一的数据，整块还会被丢弃）
            ladder_match = re.search(r'(?:登神之路|封神之路|登神指路)[:：]?\s*([+-]?)\s*(\d+)', part)
            if ladder_match:
                sign = -1 if ladder_match.group(1) == '-' else 1
                ladder_delta = sign * int(ladder_match.group(2))
            else:
                ladder_delta = 0

            # 提取觐见之梯分数（号与冒号同样可选）
            pilgrimage_match = re.search(r'觐见之梯[:：]?\s*([+-]?)\s*(\d+)', part)
            if pilgrimage_match:
                sign = -1 if pilgrimage_match.group(1) == '-' else 1
                pilgrimage_delta = sign * int(pilgrimage_match.group(2))
            else:
                pilgrimage_delta = 0

            # 提取道具（【获得道具：名称】，空格分隔多个道具，数量写作 *N 或 ×N）
            # 捕获限制在单行且遇 【 即止：否则一个缺 】 的畸形块会吞掉后续
            # 「【登神之路+16】」并把「【登神之路+16」当成道具名
            raw_items = re.findall(r'获得道具[：:]\s*([^】\n【]+)', part)
            items = []
            for raw in raw_items:
                # 按空格分隔多个道具
                for item in raw.split():
                    item = item.strip()
                    if not item or item == "无":
                        continue
                    # 数量标记位置不限：'测试*10（b）' 与 '测试（b）*10' 都识别
                    name_part, qty = extract_item_quantity(item)
                    if qty is None:
                        qty = 1
                    if qty > MAX_ITEM_QUANTITY:
                        # 展开 qty 次会占用大量内存（*2000000 约 32MB），直接判为无效
                        logger.warning(
                            f"[BatchEntry] 数量过大已忽略: {item}（上限 {MAX_ITEM_QUANTITY}）"
                        )
                        continue
                    if name_part and qty > 0:
                        # 展开 qty 次，让下游 Counter 正确统计
                        items.extend([name_part] * qty)

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

            # 失效排行榜缓存（批量录入后排行榜可能变化）
            if success_count > 0:
                self.invalidate_leaderboard_cache(group_id)

        except Exception as e:
            logger.error(f"Batch update failed, rolling back: {e}")
            await self.db.rollback()
            # 返回空的 skipped：调用方据此区分「事务失败」与「玩家全都不存在」
            # （后者会带回具体名字）。此前把所有人塞进 skipped，界面上会被误报成
            # 「以下玩家不存在，已跳过」，掩盖了真正的数据库错误。
            return 0, [], []

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
        """赐予道具。items: [(道具名（可能含等级）, 数量), ...]，数量必须为正。"""
        player = await self.db.get_player_by_name(group_id, player_name)
        if not player:
            return False, f"玩家 {player_name} 不存在"
        if any(quantity <= 0 for _, quantity in items):
            return False, "数量必须为正整数"
        # 形如「(B)」的输入会解析出空基础名，落库后是一个没有名字的道具
        if any(not (parse_item_full_name(raw)[0] or "").strip() for raw, _ in items):
            return False, "道具名不能为空"
        details = []
        for raw_name, quantity in items:
            base_name, grade = parse_item_full_name(raw_name)
            await self.db.add_item(group_id, player.player_id, base_name, quantity, grade=grade)
            details.append(format_item_display(base_name, grade, quantity))
        await self.db.commit()
        return True, f"已赐予 {player_name}: {', '.join(details)}"

    async def take_items(self, group_id: str, player_name: str, items: List[Tuple[str, Optional[int]]]) -> Tuple[bool, str]:
        """收回道具。items: [(道具名（可能含等级）, 数量或None), ...]。None=全部收回。

        等级匹配规则：显式写了等级（如「铁剑（A级）」）就只收回那一行；没写等级时
        优先收回"无等级"那一行，没有才退而取同名中等级最低的一行。
        （同名不同等级现在是独立行，不再有"按名字匹配到哪行都行"的余地）
        """
        player = await self.db.get_player_by_name(group_id, player_name)
        if not player:
            return False, f"玩家 {player_name} 不存在"
        success_details = []
        fail_details = []
        extra_notes = []  # 「全部收回」时同名其它等级的提示
        for raw_name, quantity in items:
            base_name, grade = parse_item_full_name(raw_name)
            found_items = [
                i for i in await self.db.get_player_items(group_id, player.player_id)
                if i["item_name"] == base_name
            ]
            match = self._pick_item_row(found_items, grade)
            if not match:
                fail_details.append(f"收回失败：{player_name} 没有道具 {base_name}")
                continue
            actual_grade = match["grade"]
            actual_qty = match["quantity"]
            if quantity is not None and quantity <= 0:
                fail_details.append(f"收回失败：{base_name} 的数量必须为正整数")
                continue
            if quantity is not None and quantity > actual_qty:
                fail_details.append(f"收回失败：{player_name} 的 {base_name} 只有 {actual_qty} 个")
                continue
            if not await self.db.remove_item(group_id, player.player_id, base_name, quantity,
                                             grade=actual_grade, require_sufficient=True):
                # 预读与扣减之间被并发扣走
                fail_details.append(f"收回失败：{player_name} 的 {base_name} 数量不足（可能刚被其他操作扣走）")
                continue
            if quantity is None:
                # 全部收回，显示实际收回数量
                success_details.append(format_item_display(base_name, actual_grade, actual_qty))
                # 同名其它等级不会被这次操作碰到，明确提示，免得以为已经全清
                others = [
                    i for i in found_items
                    if (i["item_name"], i["grade"]) != (base_name, actual_grade)
                ]
                if others:
                    more = "、".join(
                        format_item_display(i["item_name"], i["grade"], i["quantity"])
                        for i in others
                    )
                    extra_notes.append(f"{base_name} 另有 {more}，本次未收回")
            else:
                success_details.append(format_item_display(base_name, actual_grade, quantity))
        await self.db.commit()

        result_msg = (
            f"已从 {player_name} 收回: {', '.join(success_details)}"
            if success_details else "\n".join(fail_details)
        )
        if extra_notes:
            result_msg += "\n（" + "；".join(extra_notes) + "，如需收回请写明等级）"
        if not success_details:
            return False, result_msg
        if not fail_details:
            return True, result_msg
        return True, result_msg + "\n" + "\n".join(fail_details)

    # === 状态 ===

    async def add_status(
        self, group_id: str, player_name: str, status_name: str, days: int,
        block_actions: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """添加状态。

        block_actions：None = 保持原有阻断项不变；字符串 = 覆盖（空串清除）。
        """
        player = await self.db.get_player_by_name(group_id, player_name)
        if not player:
            return False, f"玩家 {player_name} 不存在"
        await self.db.add_status(group_id, player.player_id, status_name, days, block_actions)
        await self.db.commit()
        tail = ""
        if block_actions is not None:
            from astrbot_plugin_faith_ladder.commands.gate import format_block_actions
            shown = format_block_actions(block_actions)
            tail = f"，禁止：{shown}" if shown else "，已清除阻断项"
        return True, f"已为 {player_name} 添加状态 [{status_name}]（持续{days}天{tail}）"

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

    @staticmethod
    def _pick_item_row(found_items: List[dict], grade: Optional[str]) -> Optional[dict]:
        """从同名道具的各等级行里挑出本次要操作的那一行。

        - 指定 grade（含 `''`＝"有括号但非标准等级"）→ 精确匹配该等级
        - 未指定等级 → 优先"无等级"那一行；没有再取等级最低的一行

        `get_player_items` 按等级**降序**返回，所以此前 `deduct_item` 直接取首项时，
        「赠送道具 Bob 铁剑」会优先扣掉 `铁剑（A级）`；`take_items` 则各写了一份
        类似逻辑。统一到这里，两处行为一致。
        """
        if grade is not None:
            return next((i for i in found_items if i["grade"] == grade), None)
        no_grade = next((i for i in found_items if i["grade"] is None), None)
        return no_grade or (found_items[-1] if found_items else None)

    async def deduct_item(
        self, group_id: str, player_id: str, player_name: str,
        raw_item_name: str, quantity: int
    ) -> Tuple[bool, str, Optional[str], Optional[str]]:
        """扣除道具。返回 (success, msg, base_name, grade)。
        输入 raw_item_name 可含等级，如 '共生噬刃（C级）'。
        未指定等级时优先扣"无等级"那一行（见 _pick_item_row），
        不会去动同名的更高等级道具。
        数量必须为正：负数量在 SQL 里会变成「增加」，等于凭空造道具。"""
        if quantity <= 0:
            return False, "数量必须为正整数", None, None
        base_name, input_grade = parse_item_full_name(raw_item_name)
        items = await self.db.get_player_items(group_id, player_id)
        match = self._pick_item_row(
            [i for i in items if i["item_name"] == base_name], input_grade
        )
        if not match:
            return False, f"没有道具: {base_name}", base_name, input_grade
        if match["quantity"] < quantity:
            return False, f"道具不足：你只有 {match['quantity']} 个 {format_item_display(base_name, match['grade'], match['quantity'])}", base_name, input_grade
        if not await self.db.remove_item(group_id, player_id, base_name, quantity,
                                         grade=match["grade"], require_sufficient=True):
            # 预读与扣减之间被并发扣走：按失败返回，调用方会提示或退还
            return False, f"扣除失败：{base_name} 数量不足（可能刚被其他操作扣走），请重试", base_name, match["grade"]
        await self.db.commit()
        return True, f"已扣除 {format_item_display(base_name, match['grade'], quantity)}", base_name, match["grade"]

    async def receive_item(
        self, group_id: str, player_id: str, player_name: str,
        item_name: str, quantity: int, grade: Optional[str] = None
    ) -> Tuple[bool, str]:
        """接收道具（接收方接受时调用）。数量必须为正，否则不落库。"""
        if quantity <= 0:
            return False, "数量必须为正整数"
        await self.db.add_item(group_id, player_id, item_name, quantity, grade=grade)
        await self.db.commit()
        return True, f"已收到 {format_item_display(item_name, grade, quantity)}"

    async def cleanup_expired_gifts(
        self, max_age_seconds: int = 240,
        notify: Optional[Callable[[str, str], Awaitable[None]]] = None,
        on_refunded: Optional[Callable[[str, str], None]] = None,
    ) -> int:
        """清理超时的待处理赠送，退回道具给发送方。返回退回数量。
        notify(group_id, text) 用于向原群发送超时通知，可选。
        on_refunded(group_id, receiver_id) 用于让调用方清理自己的内存缓存，可选。"""
        expired = await self.db.get_expired_pending_gifts(max_age_seconds)
        refunded = 0
        for gift in expired:
            try:
                items = gift["items"]
                # 先认领（删除记录）再退款：并发下只有一方能删到，避免重复退款
                if not await self.db.delete_pending_gift(gift["group_id"], gift["receiver_id"]):
                    continue
                if on_refunded:
                    try:
                        on_refunded(gift["group_id"], gift["receiver_id"])
                    except Exception as e:
                        logger.error(f"[GiftCleanup] 内存缓存清理失败: {e}")
                await self.receive_item(
                    gift["group_id"], gift["sender_id"], gift["sender_name"],
                    items["item_name"], items["quantity"], grade=items.get("grade")
                )
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
