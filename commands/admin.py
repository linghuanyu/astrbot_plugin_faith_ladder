"""
管理指令（天梯榜管理/白名单/同步白名单/帮助）与白名单自动同步事件。

实现体所在模块：`@filter.command` 装饰器与指令注册保留在 main.py。
AstrBot 只扫描插件类自身的方法来注册指令，装饰器放进 mixin 会被沿 MRO
重复扫到，导致重复注册并中断后续指令的注册（约定见 commands/__init__.py）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional, List, Dict, Tuple

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent

from astrbot_plugin_faith_ladder.message_formatter import format_help
from astrbot_plugin_faith_ladder.messages import PERMISSION_DENIED
from astrbot_plugin_faith_ladder.models import VALID_FAITHS
try:
    from astrbot.api import logger
except ImportError:  # 无 AstrBot 环境（如跑测试）时退回标准库日志
    import logging
    logger = logging.getLogger(__name__)


class AdminCommandsMixin:
    """管理指令（天梯榜管理/白名单/同步白名单/帮助）与白名单自动同步事件。"""

    async def _admin_impl(self, event: "AstrMessageEvent"):
        """管理员/诸神操作。（注册在 main.py）"""
        group_id = self._get_group_id(event)
        user_id = str(event.get_sender_id())
        is_admin = self._is_plugin_admin(event)

        args = self._get_args(event, "天梯榜管理")
        if not args:
            args = self._get_args(event, "ladderadmin") or self._get_args(event, "榜管理")

        parts = args.split()
        if not parts:
            yield event.plain_result(
                f"==天梯榜管理==\n"
                f"\n"
                f"重置/ reset <玩家名> — 重置单个玩家积分 (管理员)\n"
                f"全部重置/ resetall 确认 — 重置本群所有玩家积分 (管理员)\n"
                f"删除/ delete <玩家名> — 删除单个玩家 (诸神/管理员)\n"
                f"改名/ rename <旧名> <新名> — 改名 (诸神/管理员)\n"
                f"清空/ clear 确认 — 清空本群所有玩家和数据 (管理员)\n"
                f"清除弃誓/ clearoath <玩家名> — 清除弃誓者标记 (管理员)\n"
                f"迁移储物空间/ migrate_inventory — 迁移储物空间格式（一次性）\n"
            )
            return

        action = parts[0]

        # 中英文操作名映射
        ACTION_MAP = {
            "delete": "delete", "删除": "delete",
            "rename": "rename", "改名": "rename",
            "reset": "reset", "重置": "reset",
            "resetall": "resetall", "全部重置": "resetall", "重置全部": "resetall",
            "clear": "clear", "清空": "clear",
            "clearoath": "clearoath", "清除弃誓": "clearoath",
            "migrate_inventory": "migrate_inventory", "迁移储物空间": "migrate_inventory",
        }
        action = ACTION_MAP.get(action, action)

        # delete: whitelist or admin
        if action == "delete":
            if not await self._check_perm(event):
                yield event.plain_result(PERMISSION_DENIED["god_only"])
                return
            if len(parts) < 2:
                yield event.plain_result("用法：天梯榜管理 删除 <玩家名>")
                return
            target_name = parts[1]
            deleted = await self.db_manager.delete_player_by_name(group_id, target_name)
            if deleted:
                self.ladder_service.invalidate_leaderboard_cache(group_id)
                yield event.plain_result(f"已将玩家 {target_name} 数据在本宇宙删除。")
            else:
                yield event.plain_result(f"本宇宙未找到玩家: {target_name}")
            return

        # rename: whitelist or admin
        if action == "rename":
            if not await self._check_perm(event):
                yield event.plain_result(PERMISSION_DENIED["god_only"])
                return
            if len(parts) < 3:
                yield event.plain_result("用法：天梯榜管理 改名 <旧名> <新名>")
                return
            old_name, new_name = parts[1], parts[2]
            max_name_len = self._cfg("player_name_max_length")
            if len(new_name) > max_name_len:
                yield event.plain_result(f"玩家名过长，最长 {max_name_len} 个字符。")
                return
            success, message = await self.db_manager.rename_player_by_name(group_id, old_name, new_name)
            if success:
                # 榜单显示玩家名，改名后必须失效缓存
                self.ladder_service.invalidate_leaderboard_cache(group_id)
            yield event.plain_result(message)
            return

        # Other actions: admin only
        if not is_admin:
            yield event.plain_result(PERMISSION_DENIED["god_only"])
            return

        if action == "clearoath" and len(parts) >= 2:
            target_name = parts[1]
            target_player = await self.db_manager.get_player_by_name(group_id, target_name)
            if not target_player:
                yield event.plain_result(f"本宇宙未找到玩家: {target_name}")
                return
            updated = await self.db_manager.clear_oathbreaker(group_id, target_player.player_id)
            if not updated:
                # 玩家在查询与更新之间被删除时会走到这里，不能报成功
                yield event.plain_result(f"清除 {target_name} 的弃誓者标记失败：该玩家已不存在。")
                return
            self.ladder_service.invalidate_leaderboard_cache(group_id)
            yield event.plain_result(f"已清除 {target_name} 的弃誓者标记。")
            return

        if action == "migrate_inventory":
            count = await self.db_manager.migrate_player_items()
            yield event.plain_result(f"储物空间迁移完成，共处理 {count} 条记录。")
            return

        if action == "reset" and len(parts) >= 2:
            target_name = parts[1]
            target_player = await self.db_manager.get_player_by_name(group_id, target_name)
            if not target_player:
                yield event.plain_result(f"本宇宙未找到玩家: {target_name}")
                return
            init_ladder = self._cfg("init_ladder_score")
            init_pilgrimage = self._cfg("init_pilgrimage_score")
            updated = await self.db_manager.update_scores(
                group_id, target_player.player_id,
                -target_player.ladder_score + init_ladder,
                -target_player.pilgrimage_score + init_pilgrimage,
                user_id, "管理员重置"
            )
            if not updated:
                yield event.plain_result(f"重置 {target_name} 失败：该玩家已不存在。")
                return
            self.ladder_service.invalidate_leaderboard_cache(group_id)
            yield event.plain_result(f"已重置玩家 {target_name} 的积分（天梯: {init_ladder}, 觐见: {init_pilgrimage}）。")

        elif action == "resetall":
            # 影响全群玩家，要求二次确认，避免一条误发就重置所有人
            if len(parts) < 2 or parts[1] != "确认":
                yield event.plain_result(
                    f"此操作会重置本群所有玩家的积分，不可撤销。\n"
                    f"确认请发送：天梯榜管理 全部重置 确认"
                )
                return
            init_ladder = self._cfg("init_ladder_score")
            init_pilgrimage = self._cfg("init_pilgrimage_score")
            # 必须把配置里的初始分传下去：reset_all_scores 的默认值是硬编码的 1000/100，
            # 不改配置时看不出差别，改过初始分的群会出现「回复写 A、实际重置成 B」
            count = await self.db_manager.reset_all_scores(
                group_id, initial_ladder=init_ladder, initial_pilgrimage=init_pilgrimage
            )
            self.ladder_service.invalidate_leaderboard_cache(group_id)
            yield event.plain_result(f"已重置本群 {count} 名玩家的积分（天梯: {init_ladder}, 觐见: {init_pilgrimage}）。")

        elif action == "clear":
            # 删光本群玩家/道具/状态，同样要求二次确认
            if len(parts) < 2 or parts[1] != "确认":
                yield event.plain_result(
                    f"此操作会删除本群所有玩家及其道具、状态、积分历史，不可撤销。\n"
                    f"确认请发送：天梯榜管理 清空 确认"
                )
                return
            count = await self.db_manager.delete_all_players(group_id)
            self.ladder_service.invalidate_leaderboard_cache(group_id)
            yield event.plain_result(f"已清空本群所有数据，共删除 {count} 名玩家。")

        elif action in ("reset", "clearoath"):
            # 缺目标参数时给出用法，而不是落到下面报「未知操作」（此前的行为）
            yield event.plain_result(
                f"用法：天梯榜管理 {action} <玩家名>\n"
                f"发送「天梯榜管理」查看所有可用操作。"
            )

        else:
            yield event.plain_result(f"未知操作: {action}\n发送「天梯榜管理」查看所有可用操作。")

    async def _whitelist_impl(self, event: "AstrMessageEvent"):
        """白名单管理。（注册在 main.py）"""
        if not self._is_plugin_admin(event):
            yield event.plain_result(PERMISSION_DENIED["god_only"])
            return

        user_id = str(event.get_sender_id())
        args = self._get_args(event, "白名单")
        if not args:
            args = self._get_args(event, "whitelist") or self._get_args(event, "wl")

        parts = args.split()
        if not parts:
            yield event.plain_result(
                f"用法：白名单 <add/remove/list/setfaith/removefaith/view> [用户ID] [信仰]\n"
                f"示例：白名单 add 123456789\n"
                f"      白名单 add 123456789 沉默\n"
                f"      白名单 setfaith 123456789 湮灭\n"
                f"      白名单 view — 查看所有神明及信仰"
            )
            return

        action = parts[0]
        if action == "list" or action == "view":
            text = await self.permission_service.get_whitelist_text()
            yield event.plain_result(text)
        elif action == "add" and len(parts) >= 2:
            target_id = parts[1]
            faith = parts[2] if len(parts) >= 3 else None
            # 与 setfaith 保持一致地校验：信仰文案按 16 具体信仰索引，
            # 写入无效值时不会报错，只会在之后静默退回通用文案
            if faith is not None and faith not in VALID_FAITHS:
                yield event.plain_result(f"无效信仰。可选：{'/'.join(VALID_FAITHS)}")
                return
            _, message = await self.permission_service.add_to_whitelist(target_id, user_id, faith=faith)
            # 白名单变更后失效权限缓存（让新权限立即生效）
            self.permission_service.invalidate_cache()
            yield event.plain_result(message)
        elif action == "remove" and len(parts) >= 2:
            target_id = parts[1]
            _, message = await self.permission_service.remove_from_whitelist(target_id)
            # 白名单变更后失效权限缓存（让权限移除立即生效）
            self.permission_service.invalidate_cache()
            yield event.plain_result(message)
        elif action == "setfaith" and len(parts) >= 3:
            target_id = parts[1]
            faith = parts[2]
            # 诸神信仰用于选取信仰主题文案（FAITH_MESSAGES 按 16 具体信仰索引），
            # 因此这里必须校验具体信仰而非 6 命途
            if faith not in VALID_FAITHS:
                yield event.plain_result(f"无效信仰。可选：{'/'.join(VALID_FAITHS)}")
                return
            _, message = await self.permission_service.set_whitelist_faith(target_id, faith)
            yield event.plain_result(message)
        elif action == "removefaith" and len(parts) >= 2:
            target_id = parts[1]
            _, message = await self.permission_service.remove_whitelist_faith(target_id)
            yield event.plain_result(message)
        else:
            yield event.plain_result(f"用法：白名单 <add/remove/list/setfaith/removefaith/view> [用户ID] [信仰]")

    async def _sync_whitelist_impl(self, event: "AstrMessageEvent"):
        """同步指定群的当前成员到白名单。（注册在 main.py）"""
        if not self._is_plugin_admin(event):
            yield event.plain_result(PERMISSION_DENIED["god_only"])
            return

        target_group = self._cfg("auto_whitelist_group")
        if not target_group:
            yield event.plain_result("请先在 WebUI 配置 auto_whitelist_group（诸神自动同步群号）。")
            return

        try:
            members = await event.bot.get_group_member_list(group_id=int(target_group))
        except Exception as e:
            yield event.plain_result(f"获取群成员列表失败: {e}")
            return

        bot_id = str(event.get_self_id())
        added = 0
        for member in members:
            uid = str(member.get("user_id", ""))
            if not uid or uid == bot_id:
                continue
            success, _ = await self.permission_service.add_to_whitelist(uid, "sync")
            if success:
                added += 1

        # 同步完成后失效权限缓存
        if added > 0:
            self.permission_service.invalidate_cache()

        yield event.plain_result(f"诸神列表同步完成: 新增 {added} 人（群 {target_group} 共 {len(members)} 名成员）")

    async def _help_impl(self, event: "AstrMessageEvent"):
        """显示帮助信息（注册在 main.py）"""
        text = format_help(dict(self.config))
        yield event.plain_result(text)

    async def _group_member_change_impl(self, event: "AstrMessageEvent"):
        """监听群成员变动事件，自动同步白名单。（注册在 main.py）"""
        try:
            # 检查是否为 aiocqhttp 的 notice 事件
            raw = getattr(event.message_obj, 'raw_message', None) or {}
            notice_type = raw.get('notice_type', '')
            group_id = str(raw.get('group_id', ''))
            user_id = str(raw.get('user_id', ''))

            target_group = self._cfg("auto_whitelist_group")
            if not target_group or group_id != target_group or not user_id:
                return

            bot_id = str(event.get_self_id()) if hasattr(event, 'get_self_id') else ''
            if user_id == bot_id:
                return

            if notice_type == 'group_increase':
                await self._handle_auto_whitelist(user_id, "join")
            elif notice_type == 'group_decrease':
                await self._handle_auto_whitelist(user_id, "leave")
        except Exception as e:
            logger.error(f"[AutoWhitelist] 处理成员变动事件失败: {e}")

    async def _handle_auto_whitelist(self, user_id: str, action: str):
        """处理白名单自动同步（加入/离开指定群）。"""
        target_group = self._cfg("auto_whitelist_group")
        if not target_group:
            return

        if action == "join":
            success, msg = await self.permission_service.add_to_whitelist(user_id, "auto")
            if success:
                logger.info(f"[AutoWhitelist] 自动添加白名单: {user_id}")
                self.permission_service.invalidate_cache(user_id)
        elif action == "leave":
            success, msg = await self.permission_service.remove_from_whitelist(user_id)
            if success:
                logger.info(f"[AutoWhitelist] 自动移除白名单: {user_id}")
                self.permission_service.invalidate_cache(user_id)
