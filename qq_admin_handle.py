"""
QQ 群管理命令逻辑。
由 main.py 中的 @filter.command 壳调用。
需要 aiocqhttp (OneBot11) 协议支持。
"""

import re
from astrbot.api.event import AstrMessageEvent
from astrbot.api import logger


class QQAdminHandler:
    """QQ 群管命令实现。"""

    def __init__(self, plugin):
        self.plugin = plugin

    @property
    def permission_service(self):
        return self.plugin.permission_service

    def _is_plugin_admin(self, event: AstrMessageEvent) -> bool:
        return self.plugin._is_plugin_admin(event)

    # === 辅助方法 ===

    def _get_at_targets(self, event: AstrMessageEvent) -> list:
        """提取消息中所有 @ 的 QQ 号列表。"""
        targets = []
        try:
            for seg in event.message_obj.message:
                if seg.type == "at":
                    qq = str(seg.data.get("qq", ""))
                    if qq and qq != "all":
                        targets.append(qq)
        except (AttributeError, TypeError):
            pass
        return targets

    def _parse_seconds(self, event: AstrMessageEvent, default: int = 60) -> int:
        """从消息文本中提取秒数（第一个数字）。"""
        text = event.message_str.strip()
        match = re.search(r'(\d+)', text)
        if match:
            return int(match.group(1))
        return default

    async def _check_permission(self, event: AstrMessageEvent) -> bool:
        """检查用户是否有群管权限（复用白名单系统）。"""
        user_id = str(event.get_sender_id())
        has_perm = await self.permission_service.check_score_permission(user_id)
        if has_perm:
            return True
        return self._is_plugin_admin(event)

    def _is_group_message(self, event: AstrMessageEvent) -> bool:
        try:
            return bool(event.message_obj.group_id)
        except AttributeError:
            return False

    async def _get_member_info(self, event: AstrMessageEvent, user_id: str) -> dict:
        try:
            group_id = int(event.message_obj.group_id)
            return await event.bot.get_group_member_info(
                group_id=group_id, user_id=int(user_id)
            )
        except Exception:
            return {}

    async def _get_nickname(self, event: AstrMessageEvent, user_id: str) -> str:
        member = await self._get_member_info(event, user_id)
        card = member.get("card", "")
        if card:
            return card
        return member.get("nickname", str(user_id))

    async def _get_member_role(self, event: AstrMessageEvent, user_id: str) -> str:
        member = await self._get_member_info(event, user_id)
        return member.get("role", "member")

    async def _get_reply_message_id(self, event: AstrMessageEvent) -> str:
        try:
            for seg in event.message_obj.message:
                if seg.type == "reply":
                    return str(seg.data.get("id", ""))
        except (AttributeError, TypeError):
            pass
        return ""

    async def _check_target_safe(self, event: AstrMessageEvent, target_id: str) -> str:
        """检查目标是否可操作。返回空=安全，否则返回拒绝原因。"""
        role = await self._get_member_role(event, target_id)
        if role == "owner":
            return "群主不可操作"
        if role == "admin":
            return "管理员不可操作"
        return ""

    # === 命令实现 ===

    async def handle_ban(self, event: AstrMessageEvent):
        """禁言 <秒数> @用户"""
        if not self._is_group_message(event):
            yield event.plain_result("此命令仅限群聊使用。")
            return
        if not await self._check_permission(event):
            yield event.plain_result("你没有群管权限。")
            return

        targets = self._get_at_targets(event)
        if not targets:
            yield event.plain_result("请 @ 要禁言的用户。")
            return

        duration = self._parse_seconds(event, default=60)
        group_id = int(event.message_obj.group_id)

        results = []
        for uid in targets:
            block = await self._check_target_safe(event, uid)
            if block:
                nickname = await self._get_nickname(event, uid)
                results.append(f"{nickname} — {block}，已跳过")
                continue
            try:
                await event.bot.set_group_ban(
                    group_id=group_id, user_id=int(uid), duration=duration
                )
                nickname = await self._get_nickname(event, uid)
                results.append(f"已禁言 {nickname} {duration}秒")
            except Exception as e:
                results.append(f"禁言 {uid} 失败: {e}")

        yield event.plain_result("\n".join(results))

    async def handle_unban(self, event: AstrMessageEvent):
        """解禁 @用户"""
        if not self._is_group_message(event):
            yield event.plain_result("此命令仅限群聊使用。")
            return
        if not await self._check_permission(event):
            yield event.plain_result("你没有群管权限。")
            return

        targets = self._get_at_targets(event)
        if not targets:
            yield event.plain_result("请 @ 要解禁的用户。")
            return

        group_id = int(event.message_obj.group_id)
        results = []
        for uid in targets:
            try:
                await event.bot.set_group_ban(
                    group_id=group_id, user_id=int(uid), duration=0
                )
                nickname = await self._get_nickname(event, uid)
                results.append(f"已解禁 {nickname}")
            except Exception as e:
                results.append(f"解禁 {uid} 失败: {e}")

        yield event.plain_result("\n".join(results))

    async def handle_kick(self, event: AstrMessageEvent):
        """踢人 @用户"""
        if not self._is_group_message(event):
            yield event.plain_result("此命令仅限群聊使用。")
            return
        if not await self._check_permission(event):
            yield event.plain_result("你没有群管权限。")
            return

        targets = self._get_at_targets(event)
        if not targets:
            yield event.plain_result("请 @ 要踢出的用户。")
            return

        group_id = int(event.message_obj.group_id)
        results = []
        for uid in targets:
            block = await self._check_target_safe(event, uid)
            if block:
                nickname = await self._get_nickname(event, uid)
                results.append(f"{nickname} — {block}，已跳过")
                continue
            try:
                await event.bot.set_group_kick(
                    group_id=group_id, user_id=int(uid), reject_add_request=False
                )
                nickname = await self._get_nickname(event, uid)
                results.append(f"已踢出 {nickname}")
            except Exception as e:
                results.append(f"踢出 {uid} 失败: {e}")

        yield event.plain_result("\n".join(results))

    async def handle_recall(self, event: AstrMessageEvent):
        """撤回（引用消息）"""
        if not self._is_group_message(event):
            yield event.plain_result("此命令仅限群聊使用。")
            return
        if not await self._check_permission(event):
            yield event.plain_result("你没有群管权限。")
            return

        reply_id = await self._get_reply_message_id(event)
        if not reply_id:
            yield event.plain_result("请引用要撤回的消息。")
            return

        try:
            await event.bot.delete_msg(message_id=int(reply_id))
            yield event.plain_result("已撤回该消息。")
        except Exception as e:
            yield event.plain_result(f"撤回失败: {e}")

    async def handle_mute_all(self, event: AstrMessageEvent):
        """全员禁"""
        if not self._is_group_message(event):
            yield event.plain_result("此命令仅限群聊使用。")
            return
        if not await self._check_permission(event):
            yield event.plain_result("你没有群管权限。")
            return

        group_id = int(event.message_obj.group_id)
        try:
            await event.bot.set_group_whole_ban(group_id=group_id, enable=True)
            yield event.plain_result("已开启全员禁言。")
        except Exception as e:
            yield event.plain_result(f"操作失败: {e}")

    async def handle_unmute_all(self, event: AstrMessageEvent):
        """全员解"""
        if not self._is_group_message(event):
            yield event.plain_result("此命令仅限群聊使用。")
            return
        if not await self._check_permission(event):
            yield event.plain_result("你没有群管权限。")
            return

        group_id = int(event.message_obj.group_id)
        try:
            await event.bot.set_group_whole_ban(group_id=group_id, enable=False)
            yield event.plain_result("已关闭全员禁言。")
        except Exception as e:
            yield event.plain_result(f"操作失败: {e}")
