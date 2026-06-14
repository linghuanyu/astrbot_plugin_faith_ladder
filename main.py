"""
信仰游戏天梯排行榜 - Faith Game Ladder Plugin for AstrBot
A dual-ladder ranking system with class/faith customization for group chats.
"""

import sys
from pathlib import Path

# AstrBot 加载插件时，插件的父目录可能不在 sys.path 中
_plugin_dir = Path(__file__).parent.resolve()
_parent_dir = str(_plugin_dir.parent)
if _parent_dir not in sys.path:
    sys.path.insert(0, _parent_dir)
if str(_plugin_dir) not in sys.path:
    sys.path.insert(1, str(_plugin_dir))

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

from astrbot_plugin_faith_ladder.db_manager import DatabaseManager
from astrbot_plugin_faith_ladder.ladder_service import LadderService
from astrbot_plugin_faith_ladder.permission_service import PermissionService
from astrbot_plugin_faith_ladder.cooldown import CooldownManager
from astrbot_plugin_faith_ladder.message_formatter import format_help
from astrbot_plugin_faith_ladder.models import VALID_CLASSES, VALID_FAITHS


@register(
    "astrbot_plugin_faith_ladder",
    "custom",
    "信仰游戏天梯排行榜，双积分排名，集成职业信仰体系，支持群聊积分管理。",
    "1.0.0"
)
class FaithLadderPlugin(Star):
    """信仰游戏天梯排行榜插件。"""

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self.data_dir = self._get_data_dir()
        self.db_manager = DatabaseManager(self.data_dir)
        self.ladder_service = LadderService(self.db_manager)
        self.cooldown_manager = CooldownManager()

        try:
            self.permission_service = PermissionService(self.db_manager, dict(self.config))
        except TypeError:
            logger.warning("PermissionService does not accept config param, using fallback")
            self.permission_service = PermissionService(self.db_manager)

        self._scheduler = None

    def _get_data_dir(self) -> Path:
        data_path = None
        for method_name in ("get_data_path", "get_astrbot_data_path"):
            method = getattr(self.context, method_name, None)
            if method and callable(method):
                try:
                    result = method()
                    if result:
                        data_path = Path(result)
                        break
                except Exception:
                    continue
        if not data_path:
            plugin_parent = _plugin_dir.parent
            if plugin_parent.name == "plugins":
                data_path = plugin_parent.parent
            else:
                data_path = Path("data") / "plugin_data"
        return data_path / "astrbot_plugin_faith_ladder"

    async def initialize(self):
        await self.db_manager.initialize()
        from astrbot_plugin_faith_ladder.scheduler_service import SchedulerService

        async def send_to_group(group_id: str, text: str):
            try:
                umo = f"group:{group_id}"
                from astrbot.api.message_components import Plain
                await self.context.send_message(umo, [Plain(text=text)])
            except Exception as e:
                logger.error(f"Failed to send to group {group_id}: {e}")

        self._scheduler = SchedulerService(
            data_dir=self.data_dir,
            get_leaderboard_text=self.ladder_service.get_leaderboard_text,
            get_config=lambda: dict(self.config),
            send_to_group=send_to_group,
            get_active_groups=self.db_manager.get_active_groups,
        )
        await self._scheduler.start()
        logger.info("FaithLadder plugin initialized")

    async def terminate(self):
        if self._scheduler:
            await self._scheduler.stop()
        await self.db_manager.close()
        logger.info("FaithLadder plugin terminated")

    # === Helpers ===

    def _get_group_id(self, event: AstrMessageEvent) -> str:
        return str(event.message_obj.group_id)

    def _get_args(self, event: AstrMessageEvent, cmd_name: str) -> str:
        """Extract arguments after the command name."""
        text = event.message_str.strip()
        if text == cmd_name:
            return ""
        # Find the command name in text and return everything after it
        idx = text.find(cmd_name)
        if idx >= 0:
            return text[idx + len(cmd_name):].strip()
        return ""

    def _is_plugin_admin(self, event: AstrMessageEvent) -> bool:
        user_id = str(event.get_sender_id())
        if self.permission_service.is_admin(user_id):
            return True
        try:
            if hasattr(event, 'message_obj') and hasattr(event.message_obj, 'sender'):
                return event.message_obj.sender.role in ('admin', 'owner')
        except (AttributeError, TypeError):
            pass
        return False

    async def _reply(self, event: AstrMessageEvent, text: str):
        """Send a fixed text reply directly, bypassing AI/LLM pipeline."""
        try:
            await event.send(event.plain_result(text))
        except Exception as e:
            logger.error(f"Direct send failed: {e}")

    # === 排行榜 ===

    @filter.command("天梯榜", alias={"ladder", "ranking", "排行榜"})
    async def cmd_ladder(self, event: AstrMessageEvent, *args):
        """显示天梯排行榜"""
        group_id = self._get_group_id(event)
        limit = self.config.get("ladder_display_limit", 10)
        text = await self.ladder_service.get_leaderboard_text(group_id, limit)
        await self._reply(event, text)

    # === 觐见榜 ===

    @filter.command("觐见榜", alias={"pilgrimage", "觐见"})
    async def cmd_pilgrimage(self, event: AstrMessageEvent, *args):
        """显示觐见之梯排行榜"""
        group_id = self._get_group_id(event)
        limit = self.config.get("ladder_display_limit", 10)
        text = await self.ladder_service.get_pilgrimage_leaderboard_text(group_id, limit)
        await self._reply(event, text)

    # === 查询玩家 ===

    @filter.command("查询", alias={"query", "查看"})
    async def cmd_query(self, event: AstrMessageEvent, *args):
        """查询指定玩家的天梯分与觐见分。格式: 查询 <玩家名>"""
        group_id = self._get_group_id(event)
        user_id = str(event.get_sender_id())
        args = self._get_args(event, "查询")
        if not args:
            # Also try alias
            for alias in ("query", "查看"):
                args = self._get_args(event, alias)
                if args:
                    break

        if not args.strip():
            await self._reply(event, f"用法: 查询 <玩家名>")
            return

        cooldown_seconds = self.config.get("query_cooldown_seconds", 5)
        if not self.cooldown_manager.check_cooldown(user_id, cooldown_seconds):
            remaining = self.cooldown_manager.get_remaining(user_id, cooldown_seconds)
            await self._reply(event, f"查询冷却中，请 {remaining:.0f} 秒后再试。")
            return
        self.cooldown_manager.set_cooldown(user_id)

        target_name = args.strip()
        text = await self.ladder_service.get_player_card_by_name(group_id, target_name)
        if not text:
            await self._reply(event, f"未找到玩家: {target_name}")
            return
        await self._reply(event, text)

    # === 录入积分 ===

    @filter.command("录入积分", alias={"addscore", "加分"})
    async def cmd_add_score(self, event: AstrMessageEvent, *args):
        """录入积分变化。格式: 录入积分 <玩家名> <天梯分变化> <觐见梯变化>"""
        group_id = self._get_group_id(event)
        user_id = str(event.get_sender_id())

        has_permission = await self.permission_service.check_score_permission(user_id)
        is_admin = self._is_plugin_admin(event)
        if not has_permission and not is_admin:
            await self._reply(event, "权限不足: 您不在白名单中，无法录入积分。")
            return

        args = self._get_args(event, "录入积分")
        if not args:
            args = self._get_args(event, "addscore") or self._get_args(event, "加分")

        parts = args.split()
        if len(parts) != 3:
            await self._reply(event,
                f"用法: 录入积分 <玩家名> <天梯分变化> <觐见梯变化>\n"
                f"示例: 录入积分 张三 100 50"
            )
            return

        target_name, ladder_str, pilgrimage_str = parts

        max_name_len = self.config.get("player_name_max_length", 20)
        if len(target_name) > max_name_len:
            await self._reply(event, f"玩家名过长，最长 {max_name_len} 个字符。")
            return

        try:
            ladder_delta = int(ladder_str)
            pilgrimage_delta = int(pilgrimage_str)
        except ValueError:
            await self._reply(event, "分数必须是整数。示例: 100 50 或 -20 10")
            return

        allow_negative = self.config.get("allow_negative_scores", True)
        if not allow_negative and (ladder_delta < 0 or pilgrimage_delta < 0):
            await self._reply(event, "当前配置不允许录入负分。")
            return

        target_player = await self.db_manager.get_player_by_name(group_id, target_name)
        target_id = target_player.player_id if target_player else f"name:{target_name}"

        success, message = await self.ladder_service.add_score(
            group_id, target_id, target_name, ladder_delta, pilgrimage_delta, user_id
        )
        await self._reply(event, message)

    # === 录入玩家 ===

    @filter.command("录入玩家", alias={"register", "添加玩家"})
    async def cmd_register_player(self, event: AstrMessageEvent, *args):
        """录入新玩家。格式: 录入玩家 <姓名> <信仰> <职业> <天梯分> <觐见分>"""
        group_id = self._get_group_id(event)
        user_id = str(event.get_sender_id())

        has_permission = await self.permission_service.check_score_permission(user_id)
        is_admin = self._is_plugin_admin(event)
        if not has_permission and not is_admin:
            await self._reply(event, "权限不足: 需要白名单权限才能录入玩家。")
            return

        args = self._get_args(event, "录入玩家")
        if not args:
            args = self._get_args(event, "register") or self._get_args(event, "添加玩家")

        parts = args.split()
        if len(parts) != 5:
            await self._reply(event,
                f"用法: 录入玩家 <姓名> <信仰> <职业> <天梯分> <觐见分>\n"
                f"示例: 录入玩家 张三 存在 战士 1000 100\n"
                f"可选职业: {'/'.join(VALID_CLASSES)}\n"
                f"可选信仰: {'/'.join(VALID_FAITHS)}"
            )
            return

        player_name, faith_name, class_name, ladder_str, pilgrimage_str = parts

        max_name_len = self.config.get("player_name_max_length", 20)
        if len(player_name) > max_name_len:
            await self._reply(event, f"玩家名过长，最长 {max_name_len} 个字符。")
            return

        try:
            ladder_score = int(ladder_str)
            pilgrimage_score = int(pilgrimage_str)
        except ValueError:
            await self._reply(event, "分数必须是整数。")
            return

        success, message = await self.ladder_service.register_player(
            group_id, player_name, faith_name, class_name,
            ladder_score, pilgrimage_score, user_id
        )
        await self._reply(event, message)

    # === 设置职业 ===

    @filter.command("设置职业", alias={"setclass", "改职业"})
    async def cmd_set_class(self, event: AstrMessageEvent, *args):
        """修改玩家职业信仰。格式: 设置职业 <玩家名> <职业> <信仰>"""
        group_id = self._get_group_id(event)

        has_permission = await self.permission_service.check_score_permission(str(event.get_sender_id()))
        is_admin = self._is_plugin_admin(event)
        if not has_permission and not is_admin:
            await self._reply(event, "权限不足: 需要白名单权限才能设置职业。")
            return

        args = self._get_args(event, "设置职业")
        if not args:
            args = self._get_args(event, "setclass") or self._get_args(event, "改职业")

        parts = args.split()
        if len(parts) != 3:
            await self._reply(event,
                f"用法: 设置职业 <玩家名> <职业> <信仰>\n"
                f"可选职业: {'/'.join(VALID_CLASSES)}\n"
                f"可选信仰: {'/'.join(VALID_FAITHS)}"
            )
            return

        target_name, class_name, faith_name = parts
        target_player = await self.db_manager.get_player_by_name(group_id, target_name)
        if not target_player:
            await self._reply(event, f"未找到玩家: {target_name}")
            return

        success, message = await self.ladder_service.set_class(
            group_id, target_player.player_id, target_name, class_name, faith_name
        )
        await self._reply(event, message)

    # === 天梯榜管理 ===

    @filter.command("天梯榜管理", alias={"ladderadmin", "榜管理"})
    async def cmd_admin(self, event: AstrMessageEvent, *args):
        """管理员操作。格式: 天梯榜管理 reset <玩家名>"""
        if not self._is_plugin_admin(event):
            await self._reply(event, "权限不足: 仅管理员可执行此操作。")
            return

        group_id = self._get_group_id(event)
        args = self._get_args(event, "天梯榜管理")
        if not args:
            args = self._get_args(event, "ladderadmin") or self._get_args(event, "榜管理")

        parts = args.split()
        if not parts:
            await self._reply(event,
                f"用法: 天梯榜管理 <操作>\n"
                f"可用操作: reset <玩家名> - 重置玩家积分"
            )
            return

        action = parts[0]
        if action == "reset" and len(parts) >= 2:
            target_name = parts[1]
            target_player = await self.db_manager.get_player_by_name(group_id, target_name)
            if not target_player:
                await self._reply(event, f"未找到玩家: {target_name}")
                return
            await self.db_manager.update_scores(
                group_id, target_player.player_id,
                -target_player.ladder_score, -target_player.pilgrimage_score,
                str(event.get_sender_id()), "管理员重置"
            )
            await self._reply(event, f"已重置玩家 {target_name} 的积分。")
        else:
            await self._reply(event, f"未知操作: {action}")

    # === 白名单 ===

    @filter.command("白名单", alias={"whitelist", "wl"})
    async def cmd_whitelist(self, event: AstrMessageEvent, *args):
        """白名单管理。格式: 白名单 <add/remove/list> [类型] [ID]"""
        if not self._is_plugin_admin(event):
            await self._reply(event, "权限不足: 仅管理员可管理白名单。")
            return

        group_id = self._get_group_id(event)
        user_id = str(event.get_sender_id())
        args = self._get_args(event, "白名单")
        if not args:
            args = self._get_args(event, "whitelist") or self._get_args(event, "wl")

        parts = args.split()
        if not parts:
            await self._reply(event,
                f"用法: 白名单 <add/remove/list> [类型] [ID]\n"
                f"类型: user (用户) 或 group (群)"
            )
            return

        action = parts[0]
        if action == "list":
            text = await self.permission_service.get_whitelist_text()
            await self._reply(event, text)
        elif action == "add" and len(parts) >= 3:
            _, message = await self.permission_service.add_to_whitelist(parts[1], parts[2], user_id)
            await self._reply(event, message)
        elif action == "remove" and len(parts) >= 3:
            _, message = await self.permission_service.remove_from_whitelist(parts[1], parts[2])
            await self._reply(event, message)
        else:
            await self._reply(event, f"用法: 白名单 <add/remove/list> [类型] [ID]")

    # === 帮助 ===

    @filter.command("天梯榜帮助", alias={"ladderhelp", "帮助"})
    async def cmd_help(self, event: AstrMessageEvent, *args):
        """显示帮助信息"""
        text = format_help(dict(self.config))
        await self._reply(event, text)
