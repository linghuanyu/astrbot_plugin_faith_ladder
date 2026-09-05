"""
QQ 群管理命令逻辑。
照搬 astrbot_plugin_qqadmin 的实现，仅添加白名单权限控制。
成功操作静默（不发消息），错误情况保留提示。
需要 aiocqhttp (OneBot11) 协议支持。
"""

import asyncio
from astrbot.core.message.components import At, Reply, Plain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot_plugin_faith_ladder.messages import PERMISSION_DENIED
from astrbot_plugin_faith_ladder.faith_messages import FAITH_MESSAGES, GENERIC_GOD_MESSAGES
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
    """QQ 群管命令实现。成功显示信仰主题消息，错误保留提示。"""

    def __init__(
        self,
        check_perm_fn,  # async (user_id: str) -> bool
        check_admin_fn,  # (event) -> bool
        get_faith_fn=None,  # async (user_id: str) -> str|None
    ):
        self._check_perm = check_perm_fn
        self._is_admin = check_admin_fn
        self._get_faith = get_faith_fn or (lambda uid: None)

    async def _check_permission(self, event: AiocqhttpMessageEvent) -> bool:
        """检查用户是否有群管权限（复用白名单系统）"""
        user_id = str(event.get_sender_id())
        has_perm = await self._check_perm(user_id)
        if has_perm:
            return True
        return await self._is_admin(event)

    async def _get_faith_message(self, event: AiocqhttpMessageEvent, action: str, **kwargs) -> str:
        """获取信仰专属消息。"""
        import random
        from astrbot_plugin_faith_ladder.faith_messages import FAITH_MESSAGES, GENERIC_GOD_MESSAGES

        user_id = str(event.get_sender_id())
        faith = await self._get_faith(user_id)

        messages = FAITH_MESSAGES.get(faith, {}).get(action, [])
        if messages:
            msg = random.choice(messages)
            return msg.format(**kwargs)
        # 回退到通用消息
        generic = GENERIC_GOD_MESSAGES.get(action, [])
        if generic:
            return random.choice(generic).format(**kwargs)
        return ""

    async def _check_permission(self, event: AiocqhttpMessageEvent) -> bool:
        """检查用户是否有群管权限（复用白名单系统）"""
        user_id = str(event.get_sender_id())
        has_perm = await self._check_perm(user_id)
        if has_perm:
            return True
        return await self._is_admin(event)

    async def _get_member_info(self, event: AiocqhttpMessageEvent, user_id: str) -> dict:
        """获取群成员信息（一次 API 调用获取角色、名片、昵称）。"""
        try:
            return await event.bot.get_group_member_info(
                group_id=int(event.get_group_id()), user_id=int(user_id)
            ) or {}
        except Exception:
            return {}

    # === 禁言 ===

    async def handle_ban(self, event: AiocqhttpMessageEvent, ban_time: int = None):
        """禁言 <秒数> @用户 — 成功静默，错误保留"""
        if not await self._check_permission(event):
            yield event.plain_result(PERMISSION_DENIED)
            event.stop_event()
            return

        targets = get_ats(event)
        if not targets:
            yield event.plain_result("请 @ 要禁言的用户。")
            event.stop_event()
            return

        text = event.message_str.strip()
        duration = 60
        # 只从 Plain 文本段提取秒数，避免 @ 段中的数字被误识别
        for seg in event.get_messages():
            if isinstance(seg, Plain) and seg.text:
                for part in seg.text.split():
                    if part.isdigit():
                        duration = int(part)
                        break

        errors = []
        success_targets = []
        for uid in targets:
            info = await self._get_member_info(event, uid)
            role = info.get("role", "member")
            nickname = info.get("card") or info.get("nickname") or str(uid)

            if role in ("owner", "admin"):
                label = "群主" if role == "owner" else "管理员"
                errors.append(f"{nickname} — {label}不可操作")
                continue
            try:
                await event.bot.set_group_ban(
                    group_id=int(event.get_group_id()),
                    user_id=int(uid),
                    duration=duration,
                )
                success_targets.append(nickname)
            except Exception as e:
                errors.append(f"禁言失败：{e}")

        if errors:
            yield event.plain_result("\n".join(errors))
        elif success_targets:
            # 成功：显示信仰主题消息
            for name in success_targets:
                msg = await self._get_faith_message(event, "ban_success", target_name=name)
                if msg:
                    yield event.plain_result(msg)
                    break  # 只发一条
        event.stop_event()

    # === 解禁 ===

    async def handle_unban(self, event: AiocqhttpMessageEvent):
        """解禁 @用户 — 成功显示信仰消息"""
        if not await self._check_permission(event):
            yield event.plain_result(PERMISSION_DENIED)
            event.stop_event()
            return

        targets = get_ats(event)
        if not targets:
            yield event.plain_result("请 @ 要解禁的用户。")
            event.stop_event()
            return

        success_targets = []
        for uid in targets:
            info = await self._get_member_info(event, uid)
            nickname = info.get("card") or info.get("nickname") or str(uid)
            try:
                await event.bot.set_group_ban(
                    group_id=int(event.get_group_id()),
                    user_id=int(uid),
                    duration=0,
                )
                success_targets.append(nickname)
            except Exception:
                pass

        if success_targets:
            for name in success_targets:
                msg = await self._get_faith_message(event, "unban_success", target_name=name)
                if msg:
                    yield event.plain_result(msg)
                    break
        event.stop_event()

    # === 踢人 ===

    async def handle_kick(self, event: AiocqhttpMessageEvent):
        """踢出 @用户 — 成功显示信仰消息，错误保留"""
        if not await self._check_permission(event):
            yield event.plain_result(PERMISSION_DENIED)
            event.stop_event()
            return

        targets = get_ats(event)
        if not targets:
            yield event.plain_result("请 @ 要踢出的用户。")
            event.stop_event()
            return

        errors = []
        success_targets = []
        for uid in targets:
            info = await self._get_member_info(event, uid)
            role = info.get("role", "member")
            nickname = info.get("card") or info.get("nickname") or str(uid)

            if role in ("owner", "admin"):
                label = "群主" if role == "owner" else "管理员"
                errors.append(f"{nickname} — {label}不可操作")
                continue
            try:
                await event.bot.set_group_kick(
                    group_id=int(event.get_group_id()),
                    user_id=int(uid),
                    reject_add_request=False,
                )
                success_targets.append(nickname)
            except Exception as e:
                errors.append(f"踢出失败：{e}")

        if errors:
            yield event.plain_result("\n".join(errors))
        elif success_targets:
            for name in success_targets:
                msg = await self._get_faith_message(event, "kick_success", target_name=name)
                if msg:
                    yield event.plain_result(msg)
                    break
        event.stop_event()

    # === 撤回 ===

    async def handle_recall(self, event: AiocqhttpMessageEvent):
        """撤回消息 — 成功静默，错误保留"""
        if not await self._check_permission(event):
            yield event.plain_result(PERMISSION_DENIED)
            event.stop_event()
            return

        client = event.bot
        chain = event.get_messages()
        first_seg = chain[0]

        # 方式1: 撤回引用的消息
        if isinstance(first_seg, Reply):
            try:
                await client.delete_msg(message_id=int(first_seg.id))
                # 成功：静默
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
            # 只从 Plain 文本段提取数量
            for seg in event.get_messages():
                if isinstance(seg, Plain) and seg.text:
                    for part in seg.text.split():
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

            sem = asyncio.Semaphore(10)

            async def try_delete(message):
                if str(message["sender"]["user_id"]) not in target_ids:
                    return
                async with sem:
                    try:
                        await client.delete_msg(message_id=message["message_id"])
                    except Exception:
                        pass

            tasks = [try_delete(msg) for msg in messages]
            await asyncio.gather(*tasks)

            # 成功：静默
            event.stop_event()
            return

        yield event.plain_result("请引用消息或 @ 用户。")
        event.stop_event()

    # === 全员禁 ===

    async def handle_mute_all(self, event: AiocqhttpMessageEvent):
        """全员禁言 — 成功静默"""
        if not await self._check_permission(event):
            yield event.plain_result("此等权柄，唯执棋者方可执掌。")
            event.stop_event()
            return
        try:
            await event.bot.set_group_whole_ban(
                group_id=int(event.get_group_id()), enable=True
            )
            # 成功：静默
        except Exception as e:
            yield event.plain_result(f"操作失败: {e}")
        event.stop_event()

    # === 全员解 ===

    async def handle_unmute_all(self, event: AiocqhttpMessageEvent):
        """关闭全员禁言 — 成功静默"""
        if not await self._check_permission(event):
            yield event.plain_result("此等权柄，唯执棋者方可执掌。")
            event.stop_event()
            return
        try:
            await event.bot.set_group_whole_ban(
                group_id=int(event.get_group_id()), enable=False
            )
            # 成功：静默
        except Exception as e:
            yield event.plain_result(f"操作失败: {e}")
        event.stop_event()

    # === 设置精华 ===

    async def handle_set_essence(self, event: AiocqhttpMessageEvent):
        """设置精华消息 — 引用一条消息设置为精华，成功静默"""
        if not await self._check_permission(event):
            yield event.plain_result("此等权柄，唯执棋者方可执掌。")
            event.stop_event()
            return

        chain = event.get_messages()
        first_seg = chain[0] if chain else None

        if not isinstance(first_seg, Reply):
            yield event.plain_result("请引用要设置为精华的消息。")
            event.stop_event()
            return

        try:
            await event.bot.set_essence_msg(message_id=int(first_seg.id))
            # 成功：静默
        except Exception as e:
            yield event.plain_result(f"设置精华失败: {e}")
        event.stop_event()

    # === 移除精华 ===

    async def handle_remove_essence(self, event: AiocqhttpMessageEvent):
        """移除精华消息 — 引用一条消息移除精华，成功静默"""
        if not await self._check_permission(event):
            yield event.plain_result("此等权柄，唯执棋者方可执掌。")
            event.stop_event()
            return

        chain = event.get_messages()
        first_seg = chain[0] if chain else None

        if not isinstance(first_seg, Reply):
            yield event.plain_result("请引用要移除精华的消息。")
            event.stop_event()
            return

        try:
            await event.bot.delete_essence_msg(message_id=int(first_seg.id))
            # 成功：静默
        except Exception as e:
            yield event.plain_result(f"移除精华失败: {e}")
        event.stop_event()
