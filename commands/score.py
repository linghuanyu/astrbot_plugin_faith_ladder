"""
积分录入（单条 / 批量）。

实现体所在模块：`@filter.command` 装饰器与指令注册保留在 main.py。
AstrBot 只扫描插件类自身的方法来注册指令，装饰器放进 mixin 会被沿 MRO
重复扫到，导致重复注册并中断后续指令的注册（约定见 commands/__init__.py）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent

from astrbot_plugin_faith_ladder.messages import (
    BATCH_ALL_SUCCESS, BATCH_COOLDOWN_MSG, BATCH_PARTIAL_SKIP, PERMISSION_DENIED,
)


class ScoreCommandsMixin:
    """积分录入（单条 / 批量）。"""

    async def _add_score_impl(self, event: "AstrMessageEvent"):
        """录入积分变化。（注册在 main.py）"""
        blocked, gate_msg = await self._gate(event, None)
        if blocked:
            if gate_msg:
                yield event.plain_result(gate_msg)
            return
        group_id = self._get_group_id(event)
        user_id = str(event.get_sender_id())

        if not await self._check_perm(event):
            yield event.plain_result(PERMISSION_DENIED["god_only"])
            return

        args = self._get_args(event, "录入积分")
        if not args:
            args = self._get_args(event, "addscore") or self._get_args(event, "加分")

        parts = args.split()
        if len(parts) != 3:
            yield event.plain_result(
                f"用法：录入积分 <玩家名> <天梯分变化> <觐见梯变化>\n"
                f"示例：录入积分 张三 100 50"
            )
            return

        target_name, ladder_str, pilgrimage_str = parts

        max_name_len = self._cfg("player_name_max_length")
        if len(target_name) > max_name_len:
            yield event.plain_result(f"玩家名过长，最长 {max_name_len} 个字符。")
            return

        try:
            ladder_delta = int(ladder_str)
            pilgrimage_delta = int(pilgrimage_str)
        except ValueError:
            yield event.plain_result("分数必须是整数。示例：100 50 或 -20 10")
            return

        allow_negative = self._cfg("allow_negative_scores")
        if not allow_negative and (ladder_delta < 0 or pilgrimage_delta < 0):
            yield event.plain_result( "当前配置不允许录入负分。")
            return

        target_player = await self.db_manager.get_player_by_name(group_id, target_name)
        target_id = target_player.player_id if target_player else f"name:{target_name}"

        success, message = await self.ladder_service.add_score(
            group_id, target_id, target_name, ladder_delta, pilgrimage_delta, user_id
        )
        yield event.plain_result( message)

    async def _batch_add_score_impl(self, event: "AstrMessageEvent"):
        """批量录入积分。（注册在 main.py）"""
        blocked, gate_msg = await self._gate(event, None)
        if blocked:
            if gate_msg:
                yield event.plain_result(gate_msg)
            return
        group_id = self._get_group_id(event)
        user_id = str(event.get_sender_id())

        if not await self._check_perm(event):
            yield event.plain_result(PERMISSION_DENIED["god_only"])
            return

        # Cooldown check（先查冷却，但等参数校验通过后才真正占用 —
        # 否则一条写错的指令会白白烧掉 600 秒冷却，改对了也发不出去）
        cooldown_seconds = self._cfg("ladder_cooldown_seconds")
        cd_key = f"{user_id}:batch"
        if not self.cooldown_manager.check_cooldown(cd_key, cooldown_seconds):
            remaining = self.cooldown_manager.get_remaining(cd_key, cooldown_seconds)
            yield event.plain_result(BATCH_COOLDOWN_MSG.format(seconds=f"{remaining:.0f}"))
            return

        # Extract text after command name
        args = self._get_args(event, "批量录入")
        if not args:
            args = self._get_args(event, "batch") or self._get_args(event, "bl")

        if not args or not args.strip():
            yield event.plain_result(
                "用法：批量录入 后粘贴结算文本\n"
                "示例：批量录入 【玩家：XXX ...】【登神之路+16】【觐见之梯+2】..."
            )
            return

        # Parse the text
        parsed_list, parse_err = self.ladder_service.parse_batch_scores(args.strip())
        if parse_err:
            yield event.plain_result(f"解析失败：{parse_err}")
            return

        # 参数与文本都有效，此时才占用冷却
        self.cooldown_manager.set_cooldown(cd_key)

        # Execute batch update
        success_count, success_details, skipped = await self.ladder_service.batch_add_scores(
            group_id, parsed_list, user_id
        )

        if success_count == 0 and not skipped:
            # 零成功且零跳过：只可能是事务失败回滚（玩家全不存在时 skipped 会带回名字）
            yield event.plain_result(
                "批量录入未能写入任何数据（数据库错误，已回滚）。\n"
                "请稍后重试；若反复失败请查看日志。"
            )
            return

        # Build reply
        if skipped:
            reply_parts = [BATCH_PARTIAL_SKIP.format(
                success=success_count, skip=len(skipped)
            )]
        else:
            reply_parts = [BATCH_ALL_SUCCESS.format(count=success_count)]
        if success_details:
            reply_parts.append("\n".join(success_details))
        if skipped:
            reply_parts.append(f"\n以下玩家不存在，已跳过: {', '.join(skipped)}")

        # 自动将批量录入消息设置为精华消息（仅在确有写入时才标记）
        await self._try_set_essence(event)

        yield event.plain_result("\n".join(reply_parts))

    async def _try_set_essence(self, event: AstrMessageEvent):
        """尝试将当前消息设置为精华消息。失败时静默忽略。"""
        try:
            # 获取消息 ID（AstrBot 统一消息对象）
            message_id = getattr(event.message_obj, 'message_id', None)
            if not message_id:
                return
            await event.bot.set_essence_msg(message_id=int(message_id))
        except Exception as e:
            from astrbot.api import logger
            logger.debug(f"[Essence] 设置精华消息失败（可能无权限或不支持）: {e}")
