"""
QQ 群管理命令 Mixin。
通过 Mixin 模式挂载到 FaithLadderPlugin，共用白名单权限系统。
需要 aiocqhttp (OneBot11) 协议支持。
"""

import re
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api import logger


class QQAdminHandle:
    """QQ 群管命令 Mixin。
    self 需要有: permission_service, config, _is_plugin_admin()
    """

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

    async def _check_qq_permission(self, event: AstrMessageEvent) -> bool:
        """检查用户是否有群管权限（复用白名单系统）。"""
        user_id = str(event.get_sender_id())
        has_perm = await self.permission_service.check_score_permission(user_id)
        if has_perm:
            return True
        return self._is_plugin_admin(event)

    def _is_group_message(self, event: AstrMessageEvent) -> bool:
        """检查是否为群聊消息。"""
        try:
            return bool(event.message_obj.group_id)
        except AttributeError:
            return False

    async def _get_group_member_nickname(self, event: AstrMessageEvent, user_id: str) -> str:
        """获取群成员的昵称（名片优先，fallback 到 QQ 昵称）。"""
        try:
            group_id = int(event.message_obj.group_id)
            member = await event.bot.get_group_member_info(
                group_id=group_id, user_id=int(user_id)
            )
            card = member.get("card", "")
            if card:
                return card
            return member.get("nickname", str(user_id))
        except Exception:
            return str(user_id)

    async def _get_reply_message_id(self, event: AstrMessageEvent) -> str:
        """获取引用消息的 message_id（如果有引用）。"""
        try:
            for seg in event.message_obj.message:
                if seg.type == "reply":
                    return str(seg.data.get("id", ""))
        except (AttributeError, TypeError):
            pass
        return ""

    # === w禁言 ===

    @filter.command("w禁言")
    async def cmd_w_ban(self, event: AstrMessageEvent):
        """禁言指定用户。格式: w禁言 <秒数> @用户"""
        if not self._is_group_message(event):
            yield event.plain_result("此命令仅限群聊使用。")
            return

        if not await self._check_qq_permission(event):
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
            try:
                await event.bot.set_group_ban(
                    group_id=group_id, user_id=int(uid), duration=duration
                )
                nickname = await self._get_group_member_nickname(event, uid)
                results.append(f"已禁言 {nickname} {duration}秒")
            except Exception as e:
                results.append(f"禁言 {uid} 失败: {e}")

        yield event.plain_result("\n".join(results))

    # === w解禁 ===

    @filter.command("w解禁")
    async def cmd_w_unban(self, event: AstrMessageEvent):
        """解除禁言。格式: w解禁 @用户"""
        if not self._is_group_message(event):
            yield event.plain_result("此命令仅限群聊使用。")
            return

        if not await self._check_qq_permission(event):
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
                nickname = await self._get_group_member_nickname(event, uid)
                results.append(f"已解禁 {nickname}")
            except Exception as e:
                results.append(f"解禁 {uid} 失败: {e}")

        yield event.plain_result("\n".join(results))

    # === w踢人 ===

    @filter.command("w踢人")
    async def cmd_w_kick(self, event: AstrMessageEvent):
        """踢出群聊。格式: w踢人 @用户"""
        if not self._is_group_message(event):
            yield event.plain_result("此命令仅限群聊使用。")
            return

        if not await self._check_qq_permission(event):
            yield event.plain_result("你没有群管权限。")
            return

        targets = self._get_at_targets(event)
        if not targets:
            yield event.plain_result("请 @ 要踢出的用户。")
            return

        group_id = int(event.message_obj.group_id)

        results = []
        for uid in targets:
            try:
                await event.bot.set_group_kick(
                    group_id=group_id, user_id=int(uid), reject_add_request=False
                )
                nickname = await self._get_group_member_nickname(event, uid)
                results.append(f"已踢出 {nickname}")
            except Exception as e:
                results.append(f"踢出 {uid} 失败: {e}")

        yield event.plain_result("\n".join(results))

    # === w改名 ===

    @filter.command("w改名")
    async def cmd_w_card(self, event: AstrMessageEvent):
        """修改群名片。格式: w改名 <新昵称> @用户（无@则改自己）"""
        if not self._is_group_message(event):
            yield event.plain_result("此命令仅限群聊使用。")
            return

        if not await self._check_qq_permission(event):
            yield event.plain_result("你没有群管权限。")
            return

        group_id = int(event.message_obj.group_id)
        targets = self._get_at_targets(event)

        # 提取新昵称：消息文本中去掉命令名和 @ 后的部分
        text = event.message_str.strip()
        # 去掉命令名
        for cmd in ("w改名",):
            if text.startswith(cmd):
                text = text[len(cmd):].strip()
                break

        # 去掉 @ 部分（保留非 @ 的文本作为昵称）
        # 从 message segments 中提取纯文本
        text_parts = []
        try:
            for seg in event.message_obj.message:
                if seg.type == "text":
                    t = seg.data.get("text", "").strip()
                    # 去掉命令名
                    for cmd in ("w改名",):
                        if t.startswith(cmd):
                            t = t[len(cmd):].strip()
                    if t:
                        text_parts.append(t)
        except (AttributeError, TypeError):
            pass

        new_card = " ".join(text_parts).strip()

        if targets:
            # 改目标用户名片
            if not new_card:
                yield event.plain_result("用法: w改名 <新昵称> @用户")
                return
            uid = targets[0]
            try:
                await event.bot.set_group_card(
                    group_id=group_id, user_id=int(uid), card=new_card
                )
                nickname = await self._get_group_member_nickname(event, uid)
                yield event.plain_result(f"已修改 {nickname} 的群名片为: {new_card}")
            except Exception as e:
                yield event.plain_result(f"修改名片失败: {e}")
        else:
            # 改自己的名片
            if not new_card:
                yield event.plain_result("用法: w改名 <新昵称> 或 w改名 <新昵称> @用户")
                return
            sender_id = str(event.get_sender_id())
            try:
                await event.bot.set_group_card(
                    group_id=group_id, user_id=int(sender_id), card=new_card
                )
                yield event.plain_result(f"已修改你的群名片为: {new_card}")
            except Exception as e:
                yield event.plain_result(f"修改名片失败: {e}")

    # === w撤回 ===

    @filter.command("w撤回")
    async def cmd_w_recall(self, event: AstrMessageEvent):
        """撤回消息。格式: w撤回（引用消息）或 w撤回 @用户 [数量]"""
        if not self._is_group_message(event):
            yield event.plain_result("此命令仅限群聊使用。")
            return

        if not await self._check_qq_permission(event):
            yield event.plain_result("你没有群管权限。")
            return

        group_id = int(event.message_obj.group_id)

        # 方式1: 撤回引用的消息
        reply_id = await self._get_reply_message_id(event)
        if reply_id:
            try:
                await event.bot.delete_msg(message_id=int(reply_id))
                yield event.plain_result("已撤回该消息。")
            except Exception as e:
                yield event.plain_result(f"撤回失败: {e}")
            return

        # 方式2: 撤回 @ 用户的最近消息
        targets = self._get_at_targets(event)
        if targets:
            # 解析数量
            text = event.message_str.strip()
            count = 5  # 默认
            match = re.search(r'(\d+)', text)
            if match:
                count = min(int(match.group(1)), 10)  # 最多10条

            uid = targets[0]
            nickname = await self._get_group_member_nickname(event, uid)

            try:
                # 获取群消息历史
                history = await event.bot.call_action(
                    "get_group_msg_history",
                    group_id=group_id,
                    count=50
                )
                messages = history.get("messages", []) if isinstance(history, dict) else []

                # 过滤目标用户的消息
                target_msgs = [
                    m for m in messages
                    if str(m.get("sender", {}).get("user_id", "")) == uid
                ][:count]

                if not target_msgs:
                    yield event.plain_result(f"未找到 {nickname} 的最近消息。")
                    return

                recalled = 0
                for msg in target_msgs:
                    try:
                        await event.bot.delete_msg(message_id=int(msg.get("message_id", 0)))
                        recalled += 1
                    except Exception:
                        continue

                yield event.plain_result(f"已撤回 {nickname} 的 {recalled} 条消息。")
            except Exception as e:
                yield event.plain_result(f"撤回失败: {e}")
            return

        # 无引用也无 @
        yield event.plain_result("用法: w撤回（引用消息）或 w撤回 @用户 [数量]")

    # === w全员禁 ===

    @filter.command("w全员禁")
    async def cmd_w_mute_all(self, event: AstrMessageEvent):
        """开启全员禁言。"""
        if not self._is_group_message(event):
            yield event.plain_result("此命令仅限群聊使用。")
            return

        if not await self._check_qq_permission(event):
            yield event.plain_result("你没有群管权限。")
            return

        group_id = int(event.message_obj.group_id)
        try:
            await event.bot.set_group_whole_ban(group_id=group_id, enable=True)
            yield event.plain_result("已开启全员禁言。")
        except Exception as e:
            yield event.plain_result(f"操作失败: {e}")

    # === w全员解 ===

    @filter.command("w全员解")
    async def cmd_w_unmute_all(self, event: AstrMessageEvent):
        """关闭全员禁言。"""
        if not self._is_group_message(event):
            yield event.plain_result("此命令仅限群聊使用。")
            return

        if not await self._check_qq_permission(event):
            yield event.plain_result("你没有群管权限。")
            return

        group_id = int(event.message_obj.group_id)
        try:
            await event.bot.set_group_whole_ban(group_id=group_id, enable=False)
            yield event.plain_result("已关闭全员禁言。")
        except Exception as e:
            yield event.plain_result(f"操作失败: {e}")
