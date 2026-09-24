"""
管理指令（天梯榜管理/白名单/同步白名单/帮助）与白名单自动同步事件。

实现体所在模块：`@filter.command` 装饰器与指令注册保留在 main.py。
AstrBot 只扫描插件类自身的方法来注册指令，装饰器放进 mixin 会被沿 MRO
重复扫到，导致重复注册并中断后续指令的注册（约定见 commands/__init__.py）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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
        blocked, gate_msg = await self._gate(event, None)
        if blocked:
            if gate_msg:
                yield event.plain_result(gate_msg)
            return
        group_id = self._get_group_id(event)
        user_id = str(event.get_sender_id())
        # 管理类动作只认超管：群主/群管理员止步于诸神级，不能重置/清空本群数据
        is_admin = self._is_super_admin(event)

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
                yield event.plain_result(f"在本宇宙未寻找到（{target_name}）")
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
                yield event.plain_result(f"在本宇宙未寻找到（{target_name}）")
                return
            updated = await self.db_manager.clear_oathbreaker(group_id, target_player.player_id)
            if not updated:
                # 玩家在查询与更新之间被删除时会走到这里，不能报成功
                yield event.plain_result(f"清除 {target_name} 的弃誓者标记失败：该玩家已不在本宇宙。")
                return
            self.ladder_service.invalidate_leaderboard_cache(group_id)
            yield event.plain_result(f"已清除 {target_name} 的弃誓者标记。")
            return

        if action == "migrate_inventory":
            # force：这条指令就是"再迁一次"的入口，不能因为启动时已记过账而跳过
            count = await self.db_manager.migrate_player_items(force=True)
            yield event.plain_result(f"储物空间迁移完成，共处理 {count} 条记录。")
            return

        if action == "reset" and len(parts) >= 2:
            target_name = parts[1]
            target_player = await self.db_manager.get_player_by_name(group_id, target_name)
            if not target_player:
                yield event.plain_result(f"在本宇宙未寻找到（{target_name}）")
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
                yield event.plain_result(f"重置 {target_name} 失败：该玩家已不在本宇宙。")
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
        # 白名单是全局权限，只有超管能改；群主/群管理员不做授权人
        if not self._is_super_admin(event):
            yield event.plain_result(PERMISSION_DENIED["god_only"])
            return

        user_id = str(event.get_sender_id())
        args = self._get_args(event, "白名单")
        if not args:
            args = self._get_args(event, "whitelist") or self._get_args(event, "wl")

        parts = args.split()
        if not parts:
            yield event.plain_result(
                f"用法：白名单 <add/remove/待审/通过/全部通过 [确认]/拒绝/list/setfaith/removefaith/view> [用户ID] [信仰]\n"
                f"示例：白名单 add 123456789\n"
                f"      白名单 add 123456789 沉默\n"
                f"      白名单 setfaith 123456789 湮灭\n"
                f"      白名单 待审 — 查看入群待审名单\n"
                f"      白名单 全部通过 — 超过 5 人时会先给你看名单，再加「确认」才执行\n"
                f"      白名单 view — 查看所有神明及信仰"
            )
            return

        _USAGE = "用法：白名单 <add/remove/待审/通过/全部通过 [确认]/拒绝/list/setfaith/removefaith/view> [用户ID] [信仰]"

        action = parts[0]
        if action == "list" or action == "view":
            text = await self.permission_service.get_whitelist_text()
            yield event.plain_result(text)
        elif action in ("待审", "pending"):
            yield event.plain_result(await self.permission_service.list_pending_text())
        elif action in ("通过", "approve") and len(parts) >= 2:
            target_id = parts[1]
            _, message = await self.permission_service.approve_pending(target_id)
            # 转正即授权，缓存里若留着"不是诸神"的结论必须失效
            self.permission_service.invalidate_cache(target_id)
            yield event.plain_result(message)
        elif action in ("全部通过", "approveall"):
            # 人数超过阈值时要带「确认」：这条命令在同步之后可能等于全群封神
            confirm = len(parts) >= 2 and parts[1] == "确认"
            count, message = await self.permission_service.approve_all_pending(confirm=confirm)
            if count:
                # 一次转正多人，逐个定位不值得，整体清一次
                self.permission_service.invalidate_cache()
            yield event.plain_result(message)
        elif action in ("拒绝", "reject") and len(parts) >= 2:
            target_id = parts[1]
            _, message = await self.permission_service.reject_pending(target_id)
            yield event.plain_result(message)
        elif action == "add" and len(parts) >= 2:
            target_id = parts[1]
            faith = parts[2] if len(parts) >= 3 else None
            # 与 setfaith 保持一致地校验：信仰文案按 16 具体信仰索引，
            # 写入无效值时不会报错，只会在之后静默退回通用文案
            if faith is not None and faith not in VALID_FAITHS:
                yield event.plain_result(f"无效信仰。可选：{'/'.join(VALID_FAITHS)}")
                return
            _, message = await self.permission_service.add_to_whitelist(target_id, user_id, faith=faith)
            # 只失效被改动的这个人，不必清掉所有人的缓存
            self.permission_service.invalidate_cache(target_id)
            yield event.plain_result(message)
        elif action == "remove" and len(parts) >= 2:
            target_id = parts[1]
            _, message = await self.permission_service.remove_from_whitelist(target_id)
            # 只失效被改动的这个人，不必清掉所有人的缓存
            self.permission_service.invalidate_cache(target_id)
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
            yield event.plain_result(_USAGE)

    async def _sync_whitelist_impl(self, event: "AstrMessageEvent"):
        """同步指定群的当前成员到白名单（`同步白名单 [清理]`）。（注册在 main.py）"""
        # 同步会把群成员写成诸神，属授权动作，限超管
        if not self._is_super_admin(event):
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
        member_ids = {
            uid for uid in (str(m.get("user_id", "")) for m in members)
            if uid and uid != bot_id
        }

        try:
            # 同步只写待审、不直接授权：否则「同步白名单」一条命令就把全群封神，
            # 待审机制形同虚设。超管随后用「白名单 全部通过」确认。
            added = await self.db_manager.add_many_pending(member_ids, "sync")
        except Exception as e:
            # 批写入在单事务里，失败即整体回滚，这里如实报"没写入"
            logger.error(f"[SyncWhitelist] 批量写入失败: {e}")
            yield event.plain_result(f"同步失败，未写入任何改动: {e}")
            return

        # 「清理」是显式动作：默认只增不删——把不在群里的人一并删掉，
        # 误伤的代价（连带信仰一起消失）比留下几个陈旧条目大得多。
        prune = (self._get_args(event, "同步白名单") or "").strip() in ("清理", "prune")
        removed = await self._prune_absent_members(member_ids) if prune else 0

        # 只有「清理」真的删了人才改变了授权，此时才需要失效权限缓存；
        # 单纯写入待审不改变任何人的权限。
        if removed:
            self.permission_service.invalidate_cache()
        tail = f"，移除 {removed} 人（仅由群同步写入的）" if prune else ""
        yield event.plain_result(
            f"诸神列表同步完成: {added} 人进入待审{tail}（群 {target_group} 共 {len(members)} 名成员）；"
            f"发送「白名单 全部通过」确认，或用「白名单 待审」查看"
        )

    async def _prune_absent_members(self, member_ids: set) -> int:
        """清理「由群同步写入、但现在已不在群里」的条目，返回移除人数。

        只动 added_by ∈ {auto, sync} 的条目：手动 add 的诸神即使暂时不在群里
        也不删，否则一条「清理」就能把人手工配的名单连信仰一起抹掉。
        待审条目（只可能由入群产生）也一并按群成员对账，免得退群后阴魂不散。
        """
        rows = await self.db_manager.get_whitelist_with_faith()
        absent = [
            r["entry_id"] for r in rows
            if str(r.get("added_by") or "") in ("auto", "sync")
            and r["entry_id"] not in member_ids
        ]
        removed = await self.db_manager.remove_many_from_whitelist(absent)

        pending = await self.db_manager.get_pending_whitelist()
        stale_pending = [
            r["entry_id"] for r in pending
            if str(r.get("added_by") or "") in ("auto", "sync")
            and r["entry_id"] not in member_ids
        ]
        removed += await self.db_manager.remove_many_from_whitelist(stale_pending, "pending")
        return removed

    async def _help_impl(self, event: "AstrMessageEvent"):
        """显示帮助信息（注册在 main.py）"""
        # 帮助文本同样是该群可见的输出：未启用的群要保持"完全静默"
        blocked, gate_msg = await self._gate(event, None)
        if blocked:
            if gate_msg:
                yield event.plain_result(gate_msg)
            return
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

            # 白名单自动同步跟着群访问控制走：插件没启用的群不该动它的成员
            if self._group_access_blocked(group_id):
                return

            # 记下会话串：待审提示要用完整 umo 才能发回这个群
            self._remember_umo(group_id, event)

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
        """处理白名单自动同步（加入进待审 / 离开即移除）。"""
        target_group = self._cfg("auto_whitelist_group")
        if not target_group:
            return

        if action == "join":
            # 入群不再直接授权：先进待审，等超管确认。否则"谁能进这个群"
            # 就等于"谁能成为诸神"（白名单是全局的，进一个群即全局生效）。
            # add_pending 带守卫：已是诸神的人入群不会再拿到一条待审行
            added = await self.db_manager.add_pending(user_id, "auto")
            if added:
                logger.info(f"[AutoWhitelist] 入群待审: {user_id}")
                await self._notify_group(
                    target_group,
                    "有新成员加入，已记入待审名单；"
                    "管理员可发送「白名单 待审」查看，或用「白名单 全部通过」一次确认。",
                )
        elif action == "leave":
            # 退群仍自动撤销：user 与 pending 一并清掉
            removed = await self.db_manager.remove_whitelist_entry_everywhere(user_id)
            if removed:
                logger.info(f"[AutoWhitelist] 自动移除白名单: {user_id}")
                self.permission_service.invalidate_cache(user_id)

    async def _notify_group(self, group_id, text: str) -> None:
        """往群里发一条提示（纯发送通道）。无 context（单测）或会话串未知时静默跳过。

        与赌局播报同构：拿到不 umo 就宁可跳过，也不要发到别的地方去。
        """
        context = getattr(self, "context", None)
        if context is None or not group_id or not text:
            return
        umo = self._resolve_umo(str(group_id))
        if not umo:
            logger.warning(f"[AutoWhitelist] 群 {group_id} 的会话标识未知，跳过待审提示")
            return
        try:
            from astrbot.api.message_components import Plain

            from astrbot_plugin_faith_ladder.commands.shared import wrap_message_chain
            await context.send_message(umo, wrap_message_chain([Plain(text=text)]))
        except Exception as e:
            logger.error(f"[AutoWhitelist] 发送待审提示失败: {e}")
