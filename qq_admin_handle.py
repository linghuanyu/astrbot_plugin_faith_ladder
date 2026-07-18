"""
QQ 群管理命令逻辑。
照搬 astrbot_plugin_qqadmin 的实现，仅添加白名单权限控制。
需要 aiocqhttp (OneBot11) 协议支持。
"""

import asyncio
from astrbot.core.message.components import At, Reply, Plain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.api import logger


def get_ats(event: AiocqhttpMessageEvent) -> list:
    """获取被 at 的用户 ID 列表（排除机器人自身）"""
    return [
        str(seg.qq)
        for seg in event.get_messages()
        if isinstance(seg, At) and str(seg.qq) != event.get_self_id()
    ]


async def get_nickname(event: AiocqhttpMessageEvent, user_id) -> str:
    """获取群成员昵称（群名片 > QQ昵称 > UID）"""
    user_id = int(user_id)
    group_id = event.get_group_id()
    info = {}
    try:
        info = await event.bot.get_group_member_info(
            group_id=int(group_id), user_id=user_id
        ) or {}
    except Exception:
        pass
    if not info:
        try:
            info = await event.bot.get_stranger_info(user_id=user_id) or {}
        except Exception:
            pass
    return info.get("card") or info.get("nickname") or info.get("nick") or str(user_id)


class QQAdminHandler:
    """QQ 群管命令实现。照搬 qqadmin 逻辑，添加白名单权限检查。"""

    def __init__(self, plugin):
        self.plugin = plugin

    async def _check_permission(self, event: AiocqhttpMessageEvent) -> bool:
        """检查用户是否有群管权限（复用白名单系统）"""
        user_id = str(event.get_sender_id())
        has_perm = await self.plugin.permission_service.check_score_permission(user_id)
        if has_perm:
            return True
        return self.plugin._is_plugin_admin(event)

    async def _check_target_safe(self, event: AiocqhttpMessageEvent, target_id: str) -> str:
        """检查目标是否可操作（保护群主/管理员）"""
        try:
            info = await event.bot.get_group_member_info(
                group_id=int(event.get_group_id()), user_id=int(target_id)
            )
            role = info.get("role", "member")
            if role == "owner":
                return "群主不可操作"
            if role == "admin":
                return "管理员不可操作"
        except Exception:
            pass
        return ""

    # === 禁言 ===

    async def handle_ban(self, event: AiocqhttpMessageEvent, ban_time: int = None):
        """禁言 <秒数> @用户"""
        if not await self._check_permission(event):
            yield event.plain_result("你没有群管权限。")
            event.stop_event()
            return

        targets = get_ats(event)
        if not targets:
            yield event.plain_result("请 @ 要禁言的用户。")
            event.stop_event()
            return

        # 从消息文本提取秒数
        text = event.message_str.strip()
        duration = 60
        for part in text.split():
            if part.isdigit():
                duration = int(part)
                break

        results = []
        for uid in targets:
            # 一次 API 调用同时获取昵称和角色
            info = await self._get_member_info(event, uid)
            role = info.get("role", "member")
            nickname = info.get("card") or info.get("nickname") or str(uid)

            if role in ("owner", "admin"):
                label = "群主" if role == "owner" else "管理员"
                results.append(f"{nickname} — {label}不可操作，已跳过")
                continue
            try:
                await event.bot.set_group_ban(
                    group_id=int(event.get_group_id()),
                    user_id=int(uid),
                    duration=duration,
                )
                results.append(f"已禁言 {nickname} {duration}秒")
            except Exception as e:
                results.append(f"禁言失败: {e}")

        yield event.plain_result("\n".join(results))
        event.stop_event()

    # === 解禁 ===

    async def handle_unban(self, event: AiocqhttpMessageEvent):
        """解禁 @用户"""
        if not await self._check_permission(event):
            yield event.plain_result("你没有群管权限。")
            event.stop_event()
            return

        for uid in get_ats(event):
            try:
                await event.bot.set_group_ban(
                    group_id=int(event.get_group_id()),
                    user_id=int(uid),
                    duration=0,
                )
            except Exception:
                pass
        yield event.plain_result("已解禁")
        event.stop_event()

    # === 踢人 ===

    async def handle_kick(self, event: AiocqhttpMessageEvent):
        """踢出 @用户"""
        if not await self._check_permission(event):
            yield event.plain_result("你没有群管权限。")
            event.stop_event()
            return

        for uid in get_ats(event):
            # 一次 API 调用同时获取昵称和角色
            info = await self._get_member_info(event, uid)
            role = info.get("role", "member")
            nickname = info.get("card") or info.get("nickname") or str(uid)

            if role in ("owner", "admin"):
                label = "群主" if role == "owner" else "管理员"
                yield event.plain_result(f"{nickname} — {label}不可操作，已跳过")
                continue
            try:
                await event.bot.set_group_kick(
                    group_id=int(event.get_group_id()),
                    user_id=int(uid),
                    reject_add_request=False,
                )
                yield event.plain_result(f"已将【{uid}-{nickname}】踢出群聊")
            except Exception as e:
                yield event.plain_result(f"踢出失败: {e}")
        event.stop_event()

    # === 撤回 ===

    async def handle_recall(self, event: AiocqhttpMessageEvent):
        """撤回消息（引用消息 或 @用户批量撤回）"""
        if not await self._check_permission(event):
            yield event.plain_result("你没有群管权限。")
            event.stop_event()
            return

        client = event.bot
        chain = event.get_messages()
        first_seg = chain[0]

        # 方式1: 撤回引用的消息
        if isinstance(first_seg, Reply):
            try:
                await client.delete_msg(message_id=int(first_seg.id))
                yield event.plain_result("已撤回该消息。")
            except Exception:
                yield event.plain_result("消息已过期或不存在")
            event.stop_event()
            return

        # 方式2: 撤回 @ 用户的最近消息
        if any(isinstance(seg, At) for seg in chain):
            target_ids = get_ats(event) or [event.get_self_id()]
            target_ids = {str(uid) for uid in target_ids}

            text = event.message_str.strip()
            count = 10
            for part in text.split():
                if part.isdigit():
                    count = min(int(part), 50)
                    break

            try:
                result = await client.api.call_action(
                    "get_group_msg_history",
                    group_id=int(event.get_group_id()),
                    message_seq=0,
                    count=count,
                    reverseOrder=True,
                )
                messages = list(reversed(result.get("messages", [])))
            except Exception:
                messages = []

            delete_count = 0
            sem = asyncio.Semaphore(10)

            async def try_delete(message):
                nonlocal delete_count
                if str(message["sender"]["user_id"]) not in target_ids:
                    return
                async with sem:
                    try:
                        await client.delete_msg(message_id=message["message_id"])
                        delete_count += 1
                    except Exception:
                        pass

            tasks = [try_delete(msg) for msg in messages]
            await asyncio.gather(*tasks)

            yield event.plain_result(f"已检索{count}条消息，成功撤回{delete_count}条")
            event.stop_event()
            return

        yield event.plain_result("请引用消息或 @ 用户。")
        event.stop_event()

    # === 全员禁 ===

    async def handle_mute_all(self, event: AiocqhttpMessageEvent):
        """全员禁言"""
        if not await self._check_permission(event):
            yield event.plain_result("你没有群管权限。")
            event.stop_event()
            return
        try:
            await event.bot.set_group_whole_ban(
                group_id=int(event.get_group_id()), enable=True
            )
            yield event.plain_result("已开启全员禁言。")
        except Exception as e:
            yield event.plain_result(f"操作失败: {e}")
        event.stop_event()

    # === 全员解 ===

    async def handle_unmute_all(self, event: AiocqhttpMessageEvent):
        """关闭全员禁言"""
        if not await self._check_permission(event):
            yield event.plain_result("你没有群管权限。")
            event.stop_event()
            return
        try:
            await event.bot.set_group_whole_ban(
                group_id=int(event.get_group_id()), enable=False
            )
            yield event.plain_result("已关闭全员禁言。")
        except Exception as e:
            yield event.plain_result(f"操作失败: {e}")
        event.stop_event()
