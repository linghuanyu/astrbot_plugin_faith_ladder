"""
祷词触发。

实现体所在模块：`@filter.command` 装饰器与指令注册保留在 main.py。
AstrBot 只扫描插件类自身的方法来注册指令，装饰器放进 mixin 会被沿 MRO
重复扫到，导致重复注册并中断后续指令的注册（约定见 commands/__init__.py）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional, List, Dict, Tuple

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent

from astrbot_plugin_faith_ladder.text_utils import PRAYER_NORMALIZE_RE

from astrbot_plugin_faith_ladder.message_formatter import format_prayer_trigger
from astrbot_plugin_faith_ladder.models import VALID_CLASSES, VALID_FAITHS
try:
    from astrbot.api import logger
except ImportError:  # 无 AstrBot 环境（如跑测试）时退回标准库日志
    import logging
    logger = logging.getLogger(__name__)


class PrayerCommandsMixin:
    """祷词触发。"""

    async def _prayer_message_impl(self, event: "AstrMessageEvent"):
        """监听所有消息，检测祷词触发。（注册在 main.py）"""
        logger.debug(f"[PrayerTrigger] Message received: {event.message_str}")

        # 1. 快速过滤：必须是群消息（非私聊）
        if not hasattr(event.message_obj, 'group_id') or not event.message_obj.group_id:
            logger.debug("[PrayerTrigger] Not a group message")
            return

        group_id = self._get_group_id(event)
        logger.debug(f"[PrayerTrigger] Group: {group_id}")

        # 2. 快速过滤：群是否在配置列表中
        trigger_groups = self.config.get("prayer_trigger_groups", [])
        logger.debug(f"[PrayerTrigger] Trigger groups: {trigger_groups}")
        if group_id not in trigger_groups:
            logger.debug(f"[PrayerTrigger] Group {group_id} not in trigger list")
            return

        # 3. 获取消息纯文本
        text = event.message_str
        if not text:
            return

        # 4. 快速过滤：不是命令才处理（避免与命令冲突）
        if self._is_command_message(text):
            return

        # 5. 快速长度预过滤（含标点空格，祷词 8 字 + 最多 12 个标点 = 20）
        text_len = len(text)
        if text_len < 8 or text_len > 20:
            logger.debug(f"[PrayerTrigger] Length pre-filter: {text_len} chars, skipped")
            return

        # 6. 快速汉字计数（恰好 8 个汉字才继续）
        if self._quick_chinese_count(text) != 8:
            logger.debug("[PrayerTrigger] Chinese count != 8, skipped")
            return

        # 7. 现在才做完整归一化（仅对潜在祷词消息）
        normalized = self._normalize_prayer_text(text)
        if not normalized:
            return

        # 8. 快速匹配：是否匹配任何祷词（缓存查找，O(1)）
        matched_faith = self._prayer_cache.get(normalized)
        logger.debug(f"[PrayerTrigger] Matched faith: {matched_faith}, cache size: {len(self._prayer_cache)}")
        if not matched_faith:
            logger.debug("[PrayerTrigger] No prayer match")
            return

        # 9. 现在才解析玩家身份（昂贵操作，仅对潜在祷词消息执行）
        player = await self._resolve_self_player_lenient(event)
        logger.debug(f"[PrayerTrigger] Player resolved: {player.player_name if player else None}, specific_faith: {player.specific_faith if player else None}")
        if not player:
            logger.debug("[PrayerTrigger] Player not found")
            return

        # 10. 只在字段缺失时才获取名片补全
        sender_id = str(event.get_sender_id())
        card = ""
        if not player.specific_faith or not player.class_:
            try:
                member_info = await event.bot.get_group_member_info(group_id=int(group_id), user_id=int(sender_id))
                card = member_info.get("card", "") or member_info.get("nickname", "")
            except Exception as e:
                logger.debug(f"[PrayerTrigger] Failed to get member info: {e}")

        # 补全具体信仰
        if not player.specific_faith and card:
            sf = self._extract_specific_faith(card)
            if sf:
                await self.db_manager.set_player_specific_faith(group_id, player.player_id, sf)
                # 同步内存对象：下面马上要用它判断"有无具体信仰"，
                # 不同步的话本次祷词会被当成无信仰直接丢弃，只能等下一次触发
                player.specific_faith = sf
                logger.info(f"[PrayerTrigger] 补全信仰: {player.player_name} ← {sf}")

        # 补全职业（从名片中提取职业关键词）
        if not player.class_ and card:
            for word in self._extract_card_words(card):
                if word in VALID_CLASSES:
                    # 不要用 `player.faith or ""`：命途为空时会把空串写进命途列，
                    # 使玩家从"未设定命途"变成"命途为空字符串"
                    await self.db_manager.set_player_class(
                        group_id, player.player_id, word, player.faith
                    )
                    player.class_ = word
                    logger.info(f"[PrayerTrigger] 补全职业: {player.player_name} ← {word}")
                    break
                # 检查具体职业（按长度降序匹配，避免短职业名抢先命中）
                for specific_name, (sf, sp, sc) in self._sorted_specific_classes:
                    if word == specific_name or word.startswith(specific_name):
                        await self.db_manager.set_player_class(group_id, player.player_id, sc, sp)
                        player.class_ = sc
                        if not player.specific_faith:
                            await self.db_manager.set_player_specific_faith(group_id, player.player_id, sf)
                            # 同步内存对象，否则下面的"无具体信仰"判断会把本次触发丢掉
                            player.specific_faith = sf
                        logger.info(f"[PrayerTrigger] 补全职业: {player.player_name} ← {sc}（{sf}）")
                        break
                else:
                    continue
                break

        # 说明：这里刻意**不**自动绑定 QQ。
        # 身份可能来自"名片回退"（_resolve_self_player_lenient），而名片是玩家可自行
        # 修改的弱身份：若在回退路径上自动绑定，任何人把名片改成他人名字发一次祷词，
        # 就能永久抢占对方的 QQ 绑定，之后可用「赠送道具」取走其库存。
        # 绑定只能在强身份路径上进行（录入玩家 @用户 / 绑定QQ / 检测玩家）。

        # 检查具体信仰是否补全成功
        if not player.specific_faith:
            logger.debug("[PrayerTrigger] Player has no specific faith, skipping")
            return

        # 11. 检查今日是否已触发（DB 查询）
        if await self.db_manager.has_prayer_hit_today(group_id, player.player_id):
            return

        # 12. 随机打分
        import random
        faith_matches = player.specific_faith == matched_faith
        if faith_matches:
            display_delta = random.randint(-2, 2)   # 匹配：-2 ~ +2
        else:
            display_delta = random.randint(-2, 0)   # 不匹配（渎神）：-2 ~ 0

        # 打分开关：默认关闭时只做氛围互动，展示随机结果但不改动实际分数
        score_enabled = self.config.get("prayer_score_enabled", False)
        db_delta = display_delta if score_enabled else 0

        # 13. 记录今日已触发（DB 写入本次实际生效的分值）
        recorded = await self.db_manager.record_prayer_hit(group_id, player.player_id, db_delta)
        if not recorded:
            return  # 并发情况，已被其他请求抢先

        # 14. 加分（仅开关打开且分值非 0 时）
        if db_delta != 0:
            ok, _ = await self.ladder_service.add_score(
                group_id, player.player_id, player.player_name,
                ladder_delta=0, pilgrimage_delta=db_delta,
                operator_id="prayer_trigger",
                reason="祷词触发"
            )
            if not ok:
                return

        # 15. 回复群消息 + 阻止 AI 也响应祷词（显示随机结果）
        msg = format_prayer_trigger(player.player_name, player.specific_faith, matched_faith, display_delta, self.config)
        yield event.plain_result(msg)
        event.stop_event()

    def _build_prayer_cache(self):
        """构建祷词缓存：{归一化祷词: 具体信仰名}。启动时和配置变更时调用。"""
        self._prayer_cache = {}
        # 祷词配置按具体信仰（faith）存储，16个信仰
        for faith in VALID_FAITHS:
            key = f"prayer_text_{faith}"
            prayers = self.config.get(key, [])
            for prayer in prayers:
                normalized = self._normalize_prayer_text(prayer)
                if normalized:
                    self._prayer_cache[normalized] = faith

        # 缓存命令前缀
        cmd_keys = [
            "cmd_ladder", "cmd_pilgrimage", "cmd_query", "cmd_add_score",
            "cmd_set_class", "cmd_register_player", "cmd_admin", "cmd_whitelist",
            "cmd_help", "cmd_batch_add_score", "cmd_abandon_oath", "cmd_take_oath"
        ]
        self._command_prefixes = {
            self.config.get(key, "") for key in cmd_keys if self.config.get(key)
        }

    def _normalize_prayer_text(self, text: str) -> str:
        """去除所有标点和空格，仅保留中文字符和字母数字。"""
        return PRAYER_NORMALIZE_RE.sub('', text).strip()

    def _is_command_message(self, text: str) -> bool:
        """检查消息是否以已注册的命令前缀开头。"""
        text_stripped = text.strip()
        return any(text_stripped.startswith(prefix) for prefix in self._command_prefixes if prefix)

    def _quick_chinese_count(self, text: str) -> int:
        """快速统计汉字数量（不做完整归一化）。"""
        return sum(1 for c in text if '一' <= c <= '鿿')
