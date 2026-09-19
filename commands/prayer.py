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

from astrbot_plugin_faith_ladder.plugin_config import config_snapshot, schema_keys
from astrbot_plugin_faith_ladder.message_formatter import format_prayer_trigger
from astrbot_plugin_faith_ladder.models import VALID_CLASSES, VALID_FAITHS
try:
    from astrbot.api import logger
except ImportError:  # 无 AstrBot 环境（如跑测试）时退回标准库日志
    import logging
    logger = logging.getLogger(__name__)


def _prayer_cache_config_keys() -> List[str]:
    """参与祷词缓存构建的配置键：16 个祷词列表 + 全部 `cmd_*` 指令名前缀。

    键名直接从 schema 取，以后新增信仰或指令前缀不会被漏掉（漏了就会出现
    "改了配置但缓存没重建"的隐蔽问题）。
    """
    return [k for k in schema_keys() if k.startswith("prayer_text_")] + [
        k for k in schema_keys() if k.startswith("cmd_")
    ]


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

        # 配置可能在 WebUI 里被改过：先确保祷词表/指令前缀是当前的
        self._ensure_prayer_cache()

        # 2. 快速过滤：群是否在配置列表中
        trigger_groups = self._cfg("prayer_trigger_groups")
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

        # 8.5 过闸门（群访问控制/功能开关/状态阻断）。
        # 放在这里而不是函数开头：本实现体监听的是**所有**消息，闸门里的状态判定
        # 要查一次数据库，放在最前面会让每条闲聊都多打一次 DB；放在祷词命中之后，
        # 只有真正的祷词消息才会触发查询。
        blocked, gate_msg = await self._gate(event, "prayer")
        if blocked:
            if gate_msg:
                yield event.plain_result(gate_msg)
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
                    # 榜单会显示职业，回填后必须失效缓存（缓存默认 120 秒）
                    self.ladder_service.invalidate_leaderboard_cache(group_id)
                    logger.info(f"[PrayerTrigger] 补全职业: {player.player_name} ← {word}")
                    break
                # 检查具体职业（按长度降序匹配，避免短职业名抢先命中）
                for specific_name, (sf, sp, sc) in self._sorted_specific_classes:
                    if word == specific_name or word.startswith(specific_name):
                        await self.db_manager.set_player_class(group_id, player.player_id, sc, sp)
                        player.class_ = sc
                        # 同上：榜单会显示职业
                        self.ladder_service.invalidate_leaderboard_cache(group_id)
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

        # 12. 随机打分（区间来自配置，见 _resolve_prayer_delta_range）
        import random
        faith_matches = player.specific_faith == matched_faith
        score_min, score_max = self._resolve_prayer_delta_range(faith_matches)
        display_delta = random.randint(score_min, score_max)
        # 大成功：正好落在区间上限。区间退化成单个值（固定分值）时不算，那只是"每次都一样"
        is_crit = faith_matches and score_max > score_min and display_delta == score_max

        # 打分开关：默认关闭时只做氛围互动，展示随机结果但不改动实际分数
        score_enabled = self._cfg("prayer_score_enabled")
        db_delta = display_delta if score_enabled else 0

        # 13. 记录今日已触发（DB 写入本次实际生效的分值）
        recorded = await self.db_manager.record_prayer_hit(group_id, player.player_id, db_delta)
        if not recorded:
            return  # 并发情况，已被其他请求抢先

        # 13.5 连续天数（含今天，今天这条刚写入）
        streak = await self.db_manager.get_prayer_streak(group_id, player.player_id)

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
        msg = format_prayer_trigger(
            player.player_name, player.specific_faith, matched_faith, display_delta, self.config,
            crit=is_crit, streak=streak,
        )
        yield event.plain_result(msg)
        event.stop_event()

    def _build_prayer_cache(self):
        """构建祷词缓存：{归一化祷词: 具体信仰名} 与指令前缀集合。

        初始化时调用一次；之后由 `_ensure_prayer_cache()` 按配置快照判断是否重建，
        因此 WebUI 改完祷词文本/指令名**不需要**重载插件。
        """
        self._prayer_cache = {}
        # 祷词配置按具体信仰（faith）存储，16个信仰
        for faith in VALID_FAITHS:
            key = f"prayer_text_{faith}"
            prayers = self._cfg(key)
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
            self._cfg(key) for key in cmd_keys if self._cfg(key)
        }
        # 记下构建时的配置快照，供 _ensure_prayer_cache 比对
        self._prayer_cache_snapshot = config_snapshot(self.config, _prayer_cache_config_keys())

    def _ensure_prayer_cache(self) -> None:
        """配置变过就重建祷词/指令前缀缓存（WebUI 保存后立即生效，无需重载插件）。

        比对成本 = 读 28 个键 + 比一次元组，相比后面要做的归一化与 DB 查询可忽略。
        """
        if config_snapshot(self.config, _prayer_cache_config_keys()) != getattr(
            self, "_prayer_cache_snapshot", None
        ):
            self._build_prayer_cache()
            logger.info("[PrayerTrigger] 配置已变化，祷词缓存已重建")

    def _normalize_prayer_text(self, text: str) -> str:
        """去除所有标点和空格，仅保留中文字符和字母数字。"""
        return PRAYER_NORMALIZE_RE.sub('', text).strip()

    def _resolve_prayer_delta_range(self, faith_matches: bool) -> Tuple[int, int]:
        """祷词的分值区间（闭区间），匹配与不匹配（渎神）各一对，可在配置里改。

        配置项可能是字符串、也可能被写反（min > max），这里统一成可用的 (lo, hi)；
        值不是整数时只回落该项到默认值，避免一条坏配置让祷词触发整体报错。
        """
        if faith_matches:
            defaults = (-2, 2)
            keys = ("prayer_score_min", "prayer_score_max")
        else:
            defaults = (-2, 0)
            keys = ("prayer_blasphemy_score_min", "prayer_blasphemy_score_max")

        values = []
        for key, default in zip(keys, defaults):
            try:
                values.append(int(self._cfg(key)))
            except (TypeError, ValueError):
                values.append(default)

        lo, hi = values
        return (hi, lo) if lo > hi else (lo, hi)

    def _is_command_message(self, text: str) -> bool:
        """检查消息是否以已注册的命令前缀开头。"""
        text_stripped = text.strip()
        return any(text_stripped.startswith(prefix) for prefix in self._command_prefixes if prefix)

    def _quick_chinese_count(self, text: str) -> int:
        """快速统计汉字数量（不做完整归一化）。"""
        return sum(1 for c in text if '一' <= c <= '鿿')
