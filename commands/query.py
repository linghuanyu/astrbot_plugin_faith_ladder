"""
查询类指令的实现体：`查询` 与 `查询储物空间`。

装饰器注册在 main.py 上，本模块只提供实现体（见 commands/__init__.py 的约定）。
宿主类需具备：config / db_manager / ladder_service / permission_service /
cooldown_manager，以及 main.py 中的 _get_group_id / _get_args / _check_perm /
_get_at_user_id / _resolve_name_from_card /
_resolve_target_or_self / _resolve_self_player / _send_forward_text。
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING, Optional

from astrbot_plugin_faith_ladder.messages import PLAYER_NOT_FOUND, QUERY_COOLDOWN_MSG

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent


class QueryCommandsMixin:
    """`查询` 与 `查询储物空间` 的实现体。"""

    async def _query_impl(self, event: "AstrMessageEvent"):
        """查询玩家信息。支持查自己、诸神指定玩家名、@用户、批量查询。"""
        blocked, gate_msg = await self._gate(event, "query")
        if blocked:
            if gate_msg:
                yield event.plain_result(gate_msg)
            return
        group_id = self._get_group_id(event)
        user_id = str(event.get_sender_id())

        args = self._get_args(event, "查询")
        if not args:
            for alias in ("query", "查看"):
                args = self._get_args(event, alias)
                if args:
                    break

        # 先检测权限（超管/诸神/群主·群管理统一走 _check_perm，避免这里再抄一份判定）
        has_perm = await self._check_perm(event)
        target_name = None
        target_names = None  # 批量查询用

        if has_perm:
            # 诸神/管理员：处理 @ 或玩家名参数
            at_user_id = await self._get_at_user_id(event)
            if at_user_id:
                # @ 查询：从名片识别玩家
                try:
                    member_info = await event.bot.get_group_member_info(
                        group_id=int(group_id), user_id=int(at_user_id)
                    )
                    card = member_info.get("card") or member_info.get("nickname") or ""
                    if card:
                        target_name = await self._resolve_name_from_card(card, group_id)
                    if not target_name:
                        yield event.plain_result("无法从该用户的名片识别到玩家。")
                        return
                except Exception:
                    yield event.plain_result("获取用户信息失败。")
                    return
            else:
                # 解析玩家名参数
                args_str = args.strip() if args else ""
                # 检测是否为批量查询（多个空格分隔的名字）
                name_parts = args_str.split() if args_str else []
                if len(name_parts) > 1:
                    # 批量查询模式
                    target_names = name_parts
                else:
                    # 单查询模式
                    target_name, _, error = await self._resolve_target_or_self(event, args_str)
                    if error:
                        yield event.plain_result(error)
                        return
        else:
            # 非诸神：强制 QQ 识别（高性能，需先绑定 QQ）
            self_player = await self._resolve_self_player(event)
            if not self_player:
                yield event.plain_result(
                    "无法识别你的身份，请先让诸神使用「绑定QQ @你」完成绑定。"
                )
                return
            target_name = self_player.player_name

        cooldown_seconds = self._cfg("query_cooldown_seconds")
        cd_key = f"{user_id}:query"
        if not self.cooldown_manager.check_cooldown(cd_key, cooldown_seconds):
            remaining = self.cooldown_manager.get_remaining(cd_key, cooldown_seconds)
            yield event.plain_result(QUERY_COOLDOWN_MSG.format(seconds=f"{remaining:.0f}"))
            return
        self.cooldown_manager.set_cooldown(cd_key)

        init_ladder = self._cfg("init_ladder_score")
        init_pilgrimage = self._cfg("init_pilgrimage_score")

        # 批量查询模式
        if target_names:
            cards_text, not_found = await self.ladder_service.get_player_cards_by_names(
                group_id, target_names,
                init_ladder=init_ladder,
                init_pilgrimage=init_pilgrimage
            )
            if not cards_text and not_found:
                yield event.plain_result(
                    "".join(PLAYER_NOT_FOUND.format(name=n) for n in not_found)
                )
                return
            result_parts = []
            if cards_text:
                result_parts.append(cards_text)
            if not_found:
                result_parts.append(f"\n以下玩家不在本宇宙: {', '.join(not_found)}")
            yield event.plain_result("\n".join(result_parts))
            return

        # 单查询模式
        text = await self.ladder_service.get_player_card_by_name(
            group_id, target_name,
            init_ladder=init_ladder,
            init_pilgrimage=init_pilgrimage
        )
        if not text:
            yield event.plain_result(PLAYER_NOT_FOUND.format(name=target_name))
            return
        yield event.plain_result(text)

    async def _query_inventory_impl(self, event: "AstrMessageEvent"):
        """查看玩家储物空间。查自己（含彩蛋）或诸神批量查询。"""
        blocked, gate_msg = await self._gate(event, "query_inventory")
        if blocked:
            if gate_msg:
                yield event.plain_result(gate_msg)
            return
        group_id = self._get_group_id(event)
        user_id = str(event.get_sender_id())

        has_perm = await self._check_perm(event)

        args = self._get_args(event, "查询储物空间")
        args = args.strip() if args else ""

        if has_perm:
            # 诸神/管理员：必须指定目标
            if not args:
                yield event.plain_result("用法：查询储物空间 <玩家名> [玩家名2 ...]")
                return
            names = args.split()
        else:
            # 非诸神：强制 QQ 识别（高性能，需先绑定 QQ）
            self_player = await self._resolve_self_player(event)
            if not self_player:
                yield event.plain_result(
                    "无法识别你的身份，请先让诸神使用「绑定QQ @你」完成绑定。"
                )
                return
            names = [self_player.player_name]

            # 储物空间彩蛋：非诸神查自己时可能触发，命中后由 DB 里的窗口续着
            if self._cfg("inventory_easter_egg_enabled"):
                egg = await self._inventory_easter_egg_reply(group_id, self_player.player_id)
                if egg is not None:
                    yield event.plain_result(egg)
                    return

        results = []
        not_found = []
        for name in names:
            text = await self.ladder_service.get_inventory_text(group_id, name)
            if text is None:
                not_found.append(name)
            else:
                results.append(text)

        parts = []
        if results:
            parts.append("\n\n".join(results))
        if not_found:
            parts.append(f"\n以下玩家不在本宇宙: {', '.join(not_found)}")

        text = "\n".join(parts) if parts else "未查询到任何玩家。"

        # 默认使用合并转发，失败时回退为纯文本
        if await self._send_forward_text(event, group_id, "储物空间", text):
            return
        yield event.plain_result(text)
        event.stop_event()

    async def _inventory_easter_egg_reply(
        self, group_id: str, player_id: str
    ) -> Optional[str]:
        """储物空间彩蛋：返回要回复的文案；不该触发时返回 None。

        两段判定的顺序就是这个功能的行为契约：
        1. 先看该玩家有没有**未过期的窗口**——有就重放当初那条文案。窗口内不再掷骰，
           否则"命中后持续 N 秒"只是把 5% 变成 5%×5%；也不续期，到期时间固定在
           触发那一刻，否则玩家越查越拿不回自己的道具。
        2. 没有窗口才掷骰。文案池为空时直接跳过（`random.choice` 会抛 IndexError，
           空列表属于"静默失效"的既有约定）；命中后落窗口。

        `hold_seconds <= 0` 表示整个持续窗口机制不启用：既不读旧窗口（改配置立刻
        生效，不用等已存在的窗口自然到期），也不落新窗口，退化成"只有命中那一次
        显示"的老行为。

        文案与概率、时长都在 `_conf_schema.json` 的 default 里，这里不重复一份。
        """
        hold_seconds = self._cfg("inventory_easter_egg_hold_seconds")
        if hold_seconds > 0:
            held = await self.db_manager.get_active_inventory_easter_egg(group_id, player_id)
            if held is not None:
                return held

        messages = self._cfg("inventory_easter_egg_messages")
        if not messages:
            return None
        if random.random() >= self._cfg("inventory_easter_egg_probability"):
            return None

        message = random.choice(messages)
        if hold_seconds > 0:
            await self.db_manager.set_inventory_easter_egg(
                group_id, player_id, message, hold_seconds
            )
        return message
