"""
道具赠送（赠送/接受/拒绝）。

实现体所在模块：`@filter.command` 装饰器与指令注册保留在 main.py。
AstrBot 只扫描插件类自身的方法来注册指令，装饰器放进 mixin 会被沿 MRO
重复扫到，导致重复注册并中断后续指令的注册（约定见 commands/__init__.py）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent

try:
    from astrbot.api import logger
except ImportError:  # 无 AstrBot 环境（如跑测试）时退回标准库日志
    import logging
    logger = logging.getLogger(__name__)

from astrbot_plugin_faith_ladder.text_utils import CQ_CODE_RE



class GiftCommandsMixin:
    """道具赠送（赠送/接受/拒绝）。"""

    async def _gift_item_impl(self, event: "AstrMessageEvent"):
        """赠送道具。（注册在 main.py）"""
        blocked, gate_msg = await self._gate(event, "gift_send")
        if blocked:
            if gate_msg:
                yield event.plain_result(gate_msg)
            return
        group_id = self._get_group_id(event)

        # 发送方 = 自己（强制 QQ 绑定鉴权）
        sender_player = await self._resolve_self_player(event)
        if not sender_player:
            yield event.plain_result(
                "你尚未绑定 QQ，无法赠送道具。\n"
                "请让诸神使用「绑定QQ @你」完成绑定。"
            )
            return
        sender_name = sender_player.player_name

        args = self._get_args(event, "赠送道具")
        if not args:
            yield event.plain_result("用法：赠送道具 <接收方名> <道具名> [数量]\n示例：赠送道具 Bob 铁剑 3\n      或：赠送道具 Bob 铁剑*3")
            return

        parts = args.split(None, 1)
        if len(parts) < 2:
            yield event.plain_result("用法：赠送道具 <接收方名> <道具名> [数量]")
            return

        receiver_name, item_args = parts[0], parts[1].strip()

        # 解析道具（只支持一种）
        try:
            items = self._parse_item_args(item_args)
        except ValueError as e:
            yield event.plain_result(f"道具格式有误：{e}")
            return
        if not items:
            yield event.plain_result("未指定有效道具。格式: 道具名 [数量] 或 道具名*数量")
            return
        if len(items) > 1:
            # 此前会静默丢弃除第一件以外的道具，玩家以为都送出去了
            yield event.plain_result("一次只能赠送一种道具。")
            return

        item_raw, quantity = items[0]

        # 查找接收方
        receiver_player = await self.db_manager.get_player_by_name(group_id, receiver_name)
        if not receiver_player:
            yield event.plain_result(f"玩家 {receiver_name} 不存在。")
            return

        if sender_player.player_id == receiver_player.player_id:
            yield event.plain_result("不能赠送给自己。")
            return

        # 检查接收方是否已有待处理的赠送（以 DB 为准，顺带完成超时退款与内存同步）
        gift_key = (group_id, str(receiver_player.player_id))
        if await self._get_valid_pending_gift(group_id, str(receiver_player.player_id)):
            yield event.plain_result(f"{receiver_name} 有未处理的赠送，请先接受或拒绝后再发起新的赠送。")
            return

        # 扣除发送方道具（等级解析在 service 内完成，支持 '道具名（C级）' 形式）
        success, msg, base_name, grade = await self.ladder_service.deduct_item(
            group_id, sender_player.player_id, sender_name, item_raw, quantity
        )
        if not success:
            yield event.plain_result(msg)
            return

        # 保存待处理赠送（DB 为准，成功后写内存缓存）
        import json
        gift_data = {
            "group_id": group_id,
            "sender_id": sender_player.player_id,
            "sender_name": sender_name,
            "receiver_id": receiver_player.player_id,
            "receiver_name": receiver_name,
            "item_name": base_name,
            "grade": grade,
            "quantity": quantity,
        }
        saved = await self.db_manager.save_pending_gift(
            group_id, str(receiver_player.player_id),
            str(sender_player.player_id), sender_name, receiver_name,
            json.dumps({"item_name": base_name, "grade": grade, "quantity": quantity})
        )
        if not saved:
            # 并发下另一笔赠送已抢先落库：原样退还发送方道具，避免道具丢失
            await self.ladder_service.receive_item(
                group_id, sender_player.player_id, sender_name, base_name, quantity, grade=grade
            )
            yield event.plain_result(f"{receiver_name} 有未处理的赠送，道具已退回。")
            return

        from astrbot_plugin_faith_ladder.message_formatter import format_gift_request
        notification = format_gift_request(
            sender_name, receiver_name, base_name, grade, quantity
        )
        yield event.plain_result(
            f"已从 {sender_name} 扣除，等待 {receiver_name} 接受。\n\n{notification}"
        )

    async def _accept_gift_impl(self, event: "AstrMessageEvent"):
        """接收方接受赠送，无需参数。（注册在 main.py）"""
        blocked, gate_msg = await self._gate(event, "gift_accept")
        if blocked:
            if gate_msg:
                yield event.plain_result(gate_msg)
            return
        group_id = self._get_group_id(event)
        is_god = await self._check_perm(event)
        args = self._get_args(event, "接受道具")

        if is_god and args:
            # 诸神可指定接收方（跳过名片检测）
            at_user_id = await self._get_at_user_id(event)
            receiver_player = None
            if at_user_id:
                receiver_player, err = await self._resolve_player_by_at(group_id, at_user_id, event)
                if err:
                    yield event.plain_result(err)
                    return
            if not receiver_player:
                # 没 @ 或 @ 解析失败，从参数文本取第一个词
                cleaned = CQ_CODE_RE.sub('', args).strip()
                parts = cleaned.split()
                if parts:
                    receiver_player = await self.db_manager.get_player_by_name(group_id, parts[0])
            if not receiver_player:
                yield event.plain_result("无法识别接收方，请指定玩家名或 @ 玩家。")
                return
            receiver_id = str(receiver_player.player_id)
        else:
            # 所有人默认通过 QQ 绑定鉴权（防名片冒充）
            receiver_player = await self._resolve_self_player(event)
            if not receiver_player:
                name = await self._resolve_player_name(event)
                if name:
                    yield event.plain_result(
                        f"玩家 {name} 尚未绑定 QQ，无法接受道具。\n"
                        "请让诸神使用「绑定QQ @你」完成绑定。"
                    )
                else:
                    yield event.plain_result(
                        "你尚未绑定 QQ，无法接受道具。\n"
                        "请让诸神使用「绑定QQ @你」完成绑定。"
                    )
                return
            receiver_id = str(receiver_player.player_id)

        # 检查今日接受道具次数（可配置上限，0 为不限制）。诸神代收不受此上限约束。
        if not is_god:
            daily_limit = self._cfg("gift_daily_accept_limit")
            if daily_limit > 0:
                accept_count = await self.db_manager.count_gift_accepts_today(group_id, receiver_id)
                if accept_count >= daily_limit:
                    yield event.plain_result(f"今日接受道具次数已达上限（{daily_limit} 次/天）。")
                    return

        gift = await self._get_valid_pending_gift(group_id, receiver_id)
        if not gift:
            yield event.plain_result("没有待接受的赠送（或赠送已超时退回）。")
            return

        # 先认领（删除记录）再发放：并发下只有一方能删到，避免同一笔赠送被发放两次
        if not await self.db_manager.delete_pending_gift(group_id, receiver_id):
            yield event.plain_result("没有待接受的赠送（或赠送已被处理）。")
            return

        # 记录今日已接受道具（用于计数）。诸神不受上限约束，也不占用接收方的配额。
        if not is_god:
            if not await self.db_manager.record_gift_accept(group_id, receiver_id):
                logger.warning(
                    f"[Gift] 接受计数写入失败: group={group_id} receiver={receiver_id}"
                )

        from astrbot_plugin_faith_ladder.item_utils import format_item_display
        success, msg = await self.ladder_service.receive_item(
            gift["group_id"], gift["receiver_id"], gift["receiver_name"],
            gift["item_name"], gift["quantity"], grade=gift.get("grade")
        )

        if success:
            display = format_item_display(gift["item_name"], gift.get("grade"), gift["quantity"])
            yield event.plain_result(
                f"已接受 {gift['sender_name']} 赠送的 {display}"
            )
        else:
            yield event.plain_result(f"接受失败：{msg}")

    async def _reject_gift_impl(self, event: "AstrMessageEvent"):
        """接收方拒绝赠送，无需参数。（注册在 main.py）"""
        blocked, gate_msg = await self._gate(event, "gift_reject")
        if blocked:
            if gate_msg:
                yield event.plain_result(gate_msg)
            return
        group_id = self._get_group_id(event)
        is_god = await self._check_perm(event)
        args = self._get_args(event, "拒绝道具")

        if is_god and args:
            # 诸神可指定接收方（跳过 QQ 绑定检测）
            at_user_id = await self._get_at_user_id(event)
            receiver_player = None
            if at_user_id:
                receiver_player, err = await self._resolve_player_by_at(group_id, at_user_id, event)
                if err:
                    yield event.plain_result(err)
                    return
            if not receiver_player:
                cleaned = CQ_CODE_RE.sub('', args).strip()
                parts = cleaned.split()
                if parts:
                    receiver_player = await self.db_manager.get_player_by_name(group_id, parts[0])
            if not receiver_player:
                yield event.plain_result("无法识别接收方，请指定玩家名或 @ 玩家。")
                return
            receiver_id = str(receiver_player.player_id)
        else:
            # 所有人默认通过 QQ 绑定鉴权（防名片冒充）
            receiver_player = await self._resolve_self_player(event)
            if not receiver_player:
                name = await self._resolve_player_name(event)
                if name:
                    yield event.plain_result(
                        f"玩家 {name} 尚未绑定 QQ，无法拒绝道具。\n"
                        "请让诸神使用「绑定QQ @你」完成绑定。"
                    )
                else:
                    yield event.plain_result(
                        "你尚未绑定 QQ，无法拒绝道具。\n"
                        "请让诸神使用「绑定QQ @你」完成绑定。"
                    )
                return
            receiver_id = str(receiver_player.player_id)

        gift = await self._get_valid_pending_gift(group_id, receiver_id)
        if not gift:
            yield event.plain_result("没有待拒绝的赠送（或赠送已超时退回）。")
            return

        # 先认领（删除记录）再退回：并发下只有一方能删到，避免重复退款
        if not await self.db_manager.delete_pending_gift(group_id, receiver_id):
            yield event.plain_result("没有待拒绝的赠送（或赠送已被处理）。")
            return

        from astrbot_plugin_faith_ladder.item_utils import format_item_display
        # 退回赠送方道具
        await self.ladder_service.receive_item(
            gift["group_id"], gift["sender_id"], gift["sender_name"],
            gift["item_name"], gift["quantity"], grade=gift.get("grade")
        )

        display = format_item_display(gift["item_name"], gift.get("grade"), gift["quantity"])
        yield event.plain_result(
            f"已拒绝 {gift['sender_name']} 的赠送，{display} 已退回"
        )

    async def _get_valid_pending_gift(self, group_id: str, receiver_id: str,
                                       max_age_seconds: int = 240) -> Optional[dict]:
        """获取有效的待处理赠送（未超时）。超时则自动退回发送方并返回 None。"""

        # 从 DB 获取（含 created_at）
        db_gift = await self.db_manager.get_pending_gift(group_id, receiver_id)
        if not db_gift:
            return None

        # 检查超时
        from datetime import datetime, timezone
        created_at = datetime.strptime(db_gift["created_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - created_at).total_seconds() > max_age_seconds:
            # 超时：先认领（删除记录）再退款，避免与调度器清理并发时重复退款
            if not await self.db_manager.delete_pending_gift(group_id, receiver_id):
                return None
            items = db_gift["items"]
            await self.ladder_service.receive_item(
                group_id, db_gift["sender_id"], db_gift["sender_name"],
                items["item_name"], items["quantity"], grade=items.get("grade")
            )
            logger.info(f"[Gift] 赠送超时自动退回：{db_gift['sender_name']} -> {db_gift['receiver_name']}")
            return None

        # 未超时，构造 gift 并缓存到内存
        gift = {
            "group_id": group_id,
            "sender_id": db_gift["sender_id"],
            "sender_name": db_gift["sender_name"],
            "receiver_id": receiver_id,
            "receiver_name": db_gift["receiver_name"],
            "item_name": db_gift["items"]["item_name"],
            "grade": db_gift["items"].get("grade"),
            "quantity": db_gift["items"]["quantity"],
        }
        return gift
