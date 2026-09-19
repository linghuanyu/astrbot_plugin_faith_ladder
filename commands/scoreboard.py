"""
排行榜展示（天梯榜 / 觐见榜）。

实现体所在模块：`@filter.command` 装饰器与指令注册保留在 main.py。
AstrBot 只扫描插件类自身的方法来注册指令，装饰器放进 mixin 会被沿 MRO
重复扫到，导致重复注册并中断后续指令的注册（约定见 commands/__init__.py）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional, List, Dict, Tuple

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent

from astrbot_plugin_faith_ladder.messages import COOLDOWN_MSG, PERMISSION_DENIED


class ScoreboardCommandsMixin:
    """排行榜展示（天梯榜 / 觐见榜）。"""

    async def _ladder_impl(self, event: "AstrMessageEvent"):
        """显示天梯排行榜（需要诸神权限）（注册在 main.py）"""
        blocked, gate_msg = await self._gate(event, "scoreboard")
        if blocked:
            if gate_msg:
                yield event.plain_result(gate_msg)
            return
        user_id = str(event.get_sender_id())

        # Permission check
        if not await self._check_perm(event):
            yield event.plain_result(PERMISSION_DENIED["god_only"])
            return

        # Cooldown check
        cooldown_seconds = self._cfg("ladder_cooldown_seconds")
        cd_key = f"{user_id}:ladder"
        if not self.cooldown_manager.check_cooldown(cd_key, cooldown_seconds):
            remaining = self.cooldown_manager.get_remaining(cd_key, cooldown_seconds)
            yield event.plain_result(COOLDOWN_MSG.format(seconds=f"{remaining:.0f}"))
            return
        self.cooldown_manager.set_cooldown(cd_key)

        group_id = self._get_group_id(event)
        limit = self._cfg("ladder_display_limit")
        # 榜单门槛：低于该分的玩家不上榜（0 表示不过滤）
        min_ladder_score = self._cfg("leaderboard_min_ladder_score")
        text = await self.ladder_service.get_leaderboard_text(group_id, limit, min_ladder_score)

        # 默认使用合并转发，失败时回退为纯文本
        if await self._send_forward_text(event, group_id, "天梯榜", text):
            return
        yield event.plain_result(text)
        event.stop_event()

    async def _pilgrimage_impl(self, event: "AstrMessageEvent"):
        """显示觐见之梯排行榜（需要诸神权限）（注册在 main.py）"""
        blocked, gate_msg = await self._gate(event, "scoreboard")
        if blocked:
            if gate_msg:
                yield event.plain_result(gate_msg)
            return
        user_id = str(event.get_sender_id())

        # Permission check
        if not await self._check_perm(event):
            yield event.plain_result(PERMISSION_DENIED["god_only"])
            return

        # Cooldown check
        cooldown_seconds = self._cfg("ladder_cooldown_seconds")
        cd_key = f"{user_id}:pilgrimage"
        if not self.cooldown_manager.check_cooldown(cd_key, cooldown_seconds):
            remaining = self.cooldown_manager.get_remaining(cd_key, cooldown_seconds)
            yield event.plain_result(COOLDOWN_MSG.format(seconds=f"{remaining:.0f}"))
            return
        self.cooldown_manager.set_cooldown(cd_key)

        group_id = self._get_group_id(event)
        limit = self._cfg("ladder_display_limit")
        text = await self.ladder_service.get_pilgrimage_leaderboard_text(group_id, limit)

        # 默认使用合并转发，失败时回退为纯文本
        if await self._send_forward_text(event, group_id, "觐见榜", text):
            return
        yield event.plain_result(text)
        event.stop_event()
