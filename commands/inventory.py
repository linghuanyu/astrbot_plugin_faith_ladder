"""
储物空间与玩家状态（赐予/收回/清除/状态增删）。

实现体所在模块：`@filter.command` 装饰器与指令注册保留在 main.py。
AstrBot 只扫描插件类自身的方法来注册指令，装饰器放进 mixin 会被沿 MRO
重复扫到，导致重复注册并中断后续指令的注册（约定见 commands/__init__.py）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent

from astrbot_plugin_faith_ladder.item_utils import extract_item_quantity, format_item_display
from astrbot_plugin_faith_ladder.commands.gate import ACTION_LABELS, parse_block_actions
from astrbot_plugin_faith_ladder.messages import INVALID_ITEM_FORMAT, PERMISSION_DENIED


class InventoryCommandsMixin:
    """储物空间与玩家状态（赐予/收回/清除/状态增删）。"""

    async def _give_item_impl(self, event: "AstrMessageEvent"):
        """赐予道具。（注册在 main.py）"""
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

        args = self._get_args(event, "赐予道具")
        if not args:
            yield event.plain_result("用法：赐予道具 <玩家名> <道具> <数量> ...\n示例：赐予道具 张三 铁剑 2 生命药水 3\n      或：赐予道具 张三 铁剑*2 生命药水*3")
            return

        parts = args.split(None, 1)  # 分割为玩家名 + 剩余
        if len(parts) < 2:
            yield event.plain_result("用法：赐予道具 <玩家名> <道具> <数量> ...")
            return

        player_name = parts[0]
        try:
            items = self._parse_item_args(parts[1])
        except ValueError as e:
            yield event.plain_result(f"道具格式有误：{e}")
            return
        if not items:
            yield event.plain_result(INVALID_ITEM_FORMAT)
            return

        success, message = await self.ladder_service.give_items(group_id, player_name, items)
        if success:
            # 使用信仰主题消息
            faith = await self._get_god_faith(user_id)
            items_str = ", ".join(f"{name}×{qty}" if qty > 1 else name for name, qty in items)
            themed_msg = self._get_faith_message(faith, "give_success", items=items_str, name=player_name)
            yield event.plain_result(themed_msg if themed_msg else message)
        else:
            yield event.plain_result(message)

    async def _remove_item_impl(self, event: "AstrMessageEvent"):
        """收回道具。（注册在 main.py）"""
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

        args = self._get_args(event, "收回道具")
        if not args:
            yield event.plain_result("用法：收回道具 <玩家名> <道具*数量> ...\n      或：收回道具 <玩家名> <编号> ...（如 1 2 3）")
            return

        parts = args.split(None, 1)
        if len(parts) < 2:
            yield event.plain_result("用法：收回道具 <玩家名> <道具*数量> ...\n      或：收回道具 <玩家名> <编号> ...（如 1 2 3）")
            return

        player_name = parts[0]

        # 获取玩家储物空间
        player = await self.db_manager.get_player_by_name(group_id, player_name)
        if not player:
            yield event.plain_result(f"在本宇宙未寻找到（{player_name}）")
            return

        inventory = await self.db_manager.get_player_items(group_id, player.player_id)
        if not inventory:
            yield event.plain_result(f"{player_name} 的储物空间为空")
            return

        # 尝试解析为编号模式（纯数字）
        raw_parts = parts[1].strip().split()
        if all(p.isdigit() for p in raw_parts):
            # 编号模式：收回指定编号的道具（全部数量）
            items = []
            invalid_nums = []
            for p in raw_parts:
                num = int(p)
                if 1 <= num <= len(inventory):
                    item = inventory[num - 1]
                    # 必须带上该行的等级：同名不同等级是独立行，只传名字会退回
                    # "优先无等级行"的规则，收回的就不是玩家看到的那个编号所对应的道具。
                    # format_item_display(..., 1) 产出「铁剑（A级）」「铁剑（无等级）」
                    # 「铁剑」，都能被 parse_item_full_name 反向解析。
                    items.append(
                        (format_item_display(item["item_name"], item["grade"], 1), None)
                    )
                else:
                    invalid_nums.append(p)
            if invalid_nums:
                yield event.plain_result(f"编号 {', '.join(invalid_nums)} 超出范围（1-{len(inventory)}）")
                return
        else:
            # 道具名模式：支持 道具名*数量 / 道具名×数量 / 道具名（不带数量=全部收回）
            items = []
            bad_parts = []
            for part in raw_parts:
                name, qty = extract_item_quantity(part)
                if not name or (qty is not None and qty <= 0):
                    bad_parts.append(part)
                    continue
                items.append((name, qty))
            if bad_parts:
                yield event.plain_result(
                    f"格式有误：{'、'.join(bad_parts)}（需带道具名，数量必须为正整数）"
                )
                return
            if not items:
                yield event.plain_result("用法：收回道具 <玩家名> <道具*数量> ...\n      或：收回道具 <玩家名> <编号> ...（如 1 2 3）")
                return

        success, message = await self.ladder_service.take_items(group_id, player_name, items)
        if success:
            faith = await self._get_god_faith(user_id)
            items_str = ", ".join(f"{name}×{qty}" if qty else name for name, qty in items)
            themed_msg = self._get_faith_message(faith, "take_success", items=items_str, name=player_name)
            yield event.plain_result(themed_msg if themed_msg else message)
        else:
            yield event.plain_result(message)

    async def _clear_inventory_impl(self, event: "AstrMessageEvent"):
        """清除储物空间。（注册在 main.py）"""
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

        args = self._get_args(event, "清除储物空间")
        if not args or not args.strip():
            yield event.plain_result(
                "用法：清除储物空间 <玩家名> [道具名|全部]\n"
                "示例：清除储物空间 Alice 全部        — 清空所有道具\n"
                "      清除储物空间 Alice 共生噬刃     — 清除指定道具\n"
                "      清除储物空间 Alice 共生噬刃（C级）— 清除指定等级"
            )
            return

        parts = args.split(None, 1)
        player_name = parts[0]
        raw_name = parts[1].strip() if len(parts) > 1 else None

        # 清空全部需要「全部」关键字确认
        if raw_name is None:
            yield event.plain_result(
                f"这将清空 {player_name} 的所有道具！\n"
                f"如需确认，请发送: 清除储物空间 {player_name} 全部"
            )
            return

        if raw_name == "全部":
            raw_name = None  # 传给 service 的 None 表示清空全部

        success, message = await self.ladder_service.clear_items(group_id, player_name, raw_name)
        yield event.plain_result(message)

    async def _add_status_impl(self, event: "AstrMessageEvent"):
        """添加状态。（注册在 main.py）"""
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

        args = self._get_args(event, "添加状态")
        if not args:
            yield event.plain_result(
                "用法：添加状态 <玩家名> <状态名> <天数> [阻断=祷词,赠送]\n"
                "示例：添加状态 繁荣 虚弱 3\n"
                "      添加状态 张三 沉默 2 阻断=祷词,赠送\n"
                f"可阻断：{'、'.join(ACTION_LABELS.values())}（写「阻断=无」清除阻断）"
            )
            return

        parts = args.split()
        # 可选末段「阻断=…」：不写 = 不动该状态原有的阻断项
        block_raw = None
        if parts and parts[-1].startswith("阻断="):
            block_raw = parts[-1][len("阻断="):]
            parts = parts[:-1]

        if len(parts) < 3:
            yield event.plain_result("用法：添加状态 <玩家名> <状态名> <天数> [阻断=祷词,赠送]")
            return

        # 用 unknown 判定错误：None 同时是"没写阻断段"的正常取值，
        # 若用"结果 is None"当错误判断，不写阻断段也会被当成解析失败
        block_actions, unknown = parse_block_actions(block_raw)
        if unknown:
            yield event.plain_result(
                f"无法识别的阻断项：{'、'.join(unknown)}\n"
                f"可选：{'、'.join(ACTION_LABELS.values())}（写「阻断=无」清除阻断）"
            )
            return

        player_name = parts[0]
        # 状态名可能是多词（用空格分隔的最后一部分是天数）
        days_str = parts[-1]
        status_name = " ".join(parts[1:-1])

        try:
            days = int(days_str)
        except ValueError:
            yield event.plain_result("天数必须是整数。")
            return

        if days <= 0:
            yield event.plain_result("天数必须大于0。")
            return

        success, message = await self.ladder_service.add_status(
            group_id, player_name, status_name, days, block_actions
        )
        yield event.plain_result(message)

    async def _remove_status_impl(self, event: "AstrMessageEvent"):
        """移除状态。（注册在 main.py）"""
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

        args = self._get_args(event, "移除状态")
        if not args:
            yield event.plain_result("用法：移除状态 <玩家名> <状态名>")
            return

        parts = args.split(None, 1)
        if len(parts) < 2:
            yield event.plain_result("用法：移除状态 <玩家名> <状态名>")
            return

        player_name = parts[0]
        status_name = parts[1].strip()

        success, message = await self.ladder_service.remove_status(group_id, player_name, status_name)
        yield event.plain_result(message)

    async def _clear_status_impl(self, event: "AstrMessageEvent"):
        """清除所有状态。（注册在 main.py）"""
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

        args = self._get_args(event, "清除状态")
        if not args or not args.strip():
            yield event.plain_result("用法：清除状态 <玩家名>")
            return

        player_name = args.strip()
        success, message = await self.ladder_service.clear_statuses(group_id, player_name)
        yield event.plain_result(message)
