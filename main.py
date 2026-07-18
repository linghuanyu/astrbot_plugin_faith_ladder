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
from astrbot_plugin_faith_ladder.image_renderer import ImageRenderer
from astrbot_plugin_faith_ladder.qq_admin_handle import QQAdminHandler


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
        self.image_renderer = ImageRenderer(self)

        try:
            self.permission_service = PermissionService(
                self.db_manager,
                config_getter=lambda: dict(self.config)
            )
        except TypeError:
            logger.warning("PermissionService does not accept config_getter param, using fallback")
            try:
                self.permission_service = PermissionService(self.db_manager, dict(self.config))
            except TypeError:
                self.permission_service = PermissionService(self.db_manager)

        self._scheduler = None
        self._qq_admin = QQAdminHandler(self)

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

        async def send_to_group(group_id: str, content):
            """Send content to group. Content can be:
            - str: plain text
            - tuple ('image', bytes): image from bytes
            """
            try:
                umo = f"group:{group_id}"
                if isinstance(content, tuple) and len(content) == 2 and content[0] == "image":
                    from astrbot.api.message_components import Image
                    await self.context.send_message(umo, [Image.fromBytes(content[1])])
                else:
                    from astrbot.api.message_components import Plain
                    text = content if isinstance(content, str) else str(content)
                    await self.context.send_message(umo, [Plain(text=text)])
            except Exception as e:
                logger.error(f"Failed to send to group {group_id}: {e}")

        async def get_output_mode(group_id: str) -> str:
            """Get effective output mode for a group from DB."""
            mode = await self.db_manager.get_group_output_mode(group_id)
            return mode if mode in ("text", "image") else self.config.get("output_mode", "text")

        self._scheduler = SchedulerService(
            data_dir=self.data_dir,
            get_leaderboard_text=self.ladder_service.get_leaderboard_text,
            get_pilgrimage_text=self.ladder_service.get_pilgrimage_leaderboard_text,
            get_leaderboard_players=self.ladder_service.get_leaderboard_players,
            get_pilgrimage_players=self.ladder_service.get_pilgrimage_leaderboard_players,
            image_renderer=self.image_renderer,
            get_output_mode=get_output_mode,
            get_config=lambda: dict(self.config),
            send_to_group=send_to_group,
            get_active_groups=self.db_manager.get_active_groups,
            purge_score_history=self.db_manager.purge_old_score_history,
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

    async def _render_and_send(
        self,
        event: AstrMessageEvent,
        group_id: str,
        is_ladder: bool,
        render_func,
        get_text_func,
        limit: int
    ):
        """Render image and send, with fallback to text on failure."""
        players = await render_func(group_id, limit)
        if not players:
            yield event.plain_result("暂无排名数据。")
            return

        image_format = self.config.get("image_format", "PNG")
        image_quality = self.config.get("image_quality", 90)

        # Try image rendering (returns bytes)
        if is_ladder:
            image_bytes = await self.image_renderer.render_leaderboard_image(
                players, limit, image_format=image_format, quality=image_quality
            )
        else:
            image_bytes = await self.image_renderer.render_pilgrimage_image(
                players, limit, image_format=image_format, quality=image_quality
            )

        if image_bytes:
            from astrbot.api.message_components import Image
            yield event.chain_result([Image.fromBytes(image_bytes)])
        else:
            # Fallback to text
            text = await get_text_func(group_id, limit)
            yield event.plain_result(text + "\n[图片渲染失败，已降级为文本]")

    # === 排行榜 ===

    @filter.command("天梯榜", alias={"ladder", "ranking", "排行榜"})
    async def cmd_ladder(self, event: AstrMessageEvent):
        """显示天梯排行榜（需要白名单权限）"""
        user_id = str(event.get_sender_id())
        is_admin = self._is_plugin_admin(event)

        # Permission check
        has_permission = await self.permission_service.check_score_permission(user_id)
        if not has_permission and not is_admin:
            yield event.plain_result("权限不足：区区凡人")
            return

        # Cooldown check
        cooldown_seconds = self.config.get("ladder_cooldown_seconds", 600)
        if not self.cooldown_manager.check_cooldown(user_id, cooldown_seconds):
            remaining = self.cooldown_manager.get_remaining(user_id, cooldown_seconds)
            yield event.plain_result(f"排行榜冷却中，请 {remaining:.0f} 秒后再试。")
            return
        self.cooldown_manager.set_cooldown(user_id)

        group_id = self._get_group_id(event)
        limit = self.config.get("ladder_display_limit", 10)
        output_mode = await self.ladder_service.get_effective_output_mode(
            group_id, self.config.get("output_mode", "text")
        )

        if output_mode == "image":
            async for result in self._render_and_send(
                event, group_id,
                is_ladder=True,
                render_func=self.ladder_service.get_leaderboard_players,
                get_text_func=self.ladder_service.get_leaderboard_text,
                limit=limit
            ):
                yield result
        else:
            text = await self.ladder_service.get_leaderboard_text(group_id, limit)
            yield event.plain_result(text)

    # === 觐见榜 ===

    @filter.command("觐见榜", alias={"pilgrimage", "觐见"})
    async def cmd_pilgrimage(self, event: AstrMessageEvent):
        """显示觐见之梯排行榜（需要白名单权限）"""
        user_id = str(event.get_sender_id())
        is_admin = self._is_plugin_admin(event)

        # Permission check
        has_permission = await self.permission_service.check_score_permission(user_id)
        if not has_permission and not is_admin:
            yield event.plain_result("权限不足: 区区凡人")
            return

        # Cooldown check (shared with 天梯榜)
        cooldown_seconds = self.config.get("ladder_cooldown_seconds", 600)
        if not self.cooldown_manager.check_cooldown(user_id, cooldown_seconds):
            remaining = self.cooldown_manager.get_remaining(user_id, cooldown_seconds)
            yield event.plain_result(f"排行榜冷却中，请 {remaining:.0f} 秒后再试。")
            return
        self.cooldown_manager.set_cooldown(user_id)

        group_id = self._get_group_id(event)
        limit = self.config.get("ladder_display_limit", 10)
        output_mode = await self.ladder_service.get_effective_output_mode(
            group_id, self.config.get("output_mode", "text")
        )

        if output_mode == "image":
            async for result in self._render_and_send(
                event, group_id,
                is_ladder=False,
                render_func=self.ladder_service.get_pilgrimage_leaderboard_players,
                get_text_func=self.ladder_service.get_pilgrimage_leaderboard_text,
                limit=limit
            ):
                yield result
        else:
            text = await self.ladder_service.get_pilgrimage_leaderboard_text(group_id, limit)
            yield event.plain_result(text)

    # === 输出模式切换 ===

    @filter.command("输出模式", alias={"outputmode", "模式切换"})
    async def cmd_output_mode(self, event: AstrMessageEvent):
        """切换输出模式（仅管理员）。格式: 输出模式 <text|image>"""
        if not self._is_plugin_admin(event):
            yield event.plain_result("权限不足：仅管理员可切换输出模式。")
            return

        group_id = self._get_group_id(event)

        # Get argument
        args = self._get_args(event, "输出模式")
        if not args:
            args = self._get_args(event, "outputmode") or self._get_args(event, "模式切换")

        if not args or args not in ("text", "image"):
            current_mode = await self.ladder_service.get_effective_output_mode(
                group_id, self.config.get("output_mode", "text")
            )
            yield event.plain_result(
                f"当前群输出模式: {current_mode}\n"
                f"全局默认模式: {self.config.get('output_mode', 'text')}\n\n"
                f"用法: 输出模式 <text|image>\n"
                f"  text  - 纯文本输出\n"
                f"  image - 图片输出"
            )
            return

        # Store per-group mode in DB
        await self.db_manager.set_group_output_mode(group_id, args)
        yield event.plain_result(f"本群输出模式已切换为: {args}")

    # === 查询玩家 ===

    def _extract_name_from_card(self, card: str) -> str:
        """从群名片格式中提取玩家名。
        支持格式:
          【XX】 蓬莱 守墓人100 100  → 蓬莱
          【欺诈】name 1 1            → name
        """
        import re
        card = card.strip()
        # 先尝试去掉开头的【...】标签（可能有也可能没有后续空格）
        match = re.match(r'^【[^】]*】\s*(.*)', card)
        if match:
            remaining = match.group(1).strip()
            if remaining:
                # 标签后的第一段就是名字
                return remaining.split()[0]
        # 没有【】标签，取第一段
        parts = card.split()
        if len(parts) >= 1:
            return parts[0]
        return card

    @filter.command("查询", alias={"query", "查看"})
    async def cmd_query(self, event: AstrMessageEvent):
        """查询指定玩家信息。格式: 查询 <玩家名> 或 查询 @用户"""
        group_id = self._get_group_id(event)
        user_id = str(event.get_sender_id())

        # 检查是否有 @ 目标
        target_name = None
        try:
            from astrbot.core.message.components import At
            for seg in event.get_messages():
                if isinstance(seg, At):
                    target_uid = str(seg.qq)
                    # 获取群名片
                    try:
                        info = await event.bot.get_group_member_info(
                            group_id=int(group_id), user_id=int(target_uid)
                        )
                        card = info.get("card", "") or info.get("nickname", "")
                        if card:
                            target_name = self._extract_name_from_card(card)
                    except Exception:
                        pass
                    break
        except Exception:
            pass

        # 如果没有 @ 目标，从文本参数获取
        if not target_name:
            args = self._get_args(event, "查询")
            if not args:
                for alias in ("query", "查看"):
                    args = self._get_args(event, alias)
                    if args:
                        break
            target_name = args.strip() if args else ""

        if not target_name:
            yield event.plain_result("用法: 查询 <玩家名> 或 查询 @用户")
            return

        cooldown_seconds = self.config.get("query_cooldown_seconds", 5)
        if not self.cooldown_manager.check_cooldown(user_id, cooldown_seconds):
            remaining = self.cooldown_manager.get_remaining(user_id, cooldown_seconds)
            yield event.plain_result(f"查询冷却中，请 {remaining:.0f} 秒后再试。")
            return
        self.cooldown_manager.set_cooldown(user_id)

        text = await self.ladder_service.get_player_card_by_name(
            group_id, target_name,
            init_ladder=self.config.get("init_ladder_score", 1000),
            init_pilgrimage=self.config.get("init_pilgrimage_score", 100)
        )
        if not text:
            yield event.plain_result(f" {target_name}不属于这个宇宙")
            return
        yield event.plain_result(text)

    # === 录入积分 ===

    @filter.command("录入积分", alias={"addscore", "加分"})
    async def cmd_add_score(self, event: AstrMessageEvent):
        """录入积分变化。格式: 录入积分 <玩家名> <天梯分变化> <觐见梯变化>"""
        group_id = self._get_group_id(event)
        user_id = str(event.get_sender_id())

        has_permission = await self.permission_service.check_score_permission(user_id)
        is_admin = self._is_plugin_admin(event)
        if not has_permission and not is_admin:
            yield event.plain_result("凡人也胆敢染指神明的权柄？")
            return

        args = self._get_args(event, "录入积分")
        if not args:
            args = self._get_args(event, "addscore") or self._get_args(event, "加分")

        parts = args.split()
        if len(parts) != 3:
            yield event.plain_result(
                f"用法: 录入积分 <玩家名> <天梯分变化> <觐见梯变化>\n"
                f"示例: 录入积分 张三 100 50"
            )
            return

        target_name, ladder_str, pilgrimage_str = parts

        max_name_len = self.config.get("player_name_max_length", 20)
        if len(target_name) > max_name_len:
            yield event.plain_result(f"玩家名过长，最长 {max_name_len} 个字符。")
            return

        try:
            ladder_delta = int(ladder_str)
            pilgrimage_delta = int(pilgrimage_str)
        except ValueError:
            yield event.plain_result("分数必须是整数。示例: 100 50 或 -20 10")
            return

        allow_negative = self.config.get("allow_negative_scores", True)
        if not allow_negative and (ladder_delta < 0 or pilgrimage_delta < 0):
            yield event.plain_result( "当前配置不允许录入负分。")
            return

        target_player = await self.db_manager.get_player_by_name(group_id, target_name)
        target_id = target_player.player_id if target_player else f"name:{target_name}"

        success, message = await self.ladder_service.add_score(
            group_id, target_id, target_name, ladder_delta, pilgrimage_delta, user_id
        )
        yield event.plain_result( message)

    # === 批量录入积分 ===

    @filter.command("批量录入", alias={"batch", "bl"})
    async def cmd_batch_add_score(self, event: AstrMessageEvent):
        """批量录入积分。格式: 批量录入 后粘贴结算文本"""
        group_id = self._get_group_id(event)
        user_id = str(event.get_sender_id())

        has_permission = await self.permission_service.check_score_permission(user_id)
        is_admin = self._is_plugin_admin(event)
        if not has_permission and not is_admin:
            yield event.plain_result("凡人也胆敢染指神明的权柄？")
            return

        # Cooldown check (shared with 天梯榜)
        cooldown_seconds = self.config.get("ladder_cooldown_seconds", 600)
        if not self.cooldown_manager.check_cooldown(user_id, cooldown_seconds):
            remaining = self.cooldown_manager.get_remaining(user_id, cooldown_seconds)
            yield event.plain_result(f"批量录入冷却中，请 {remaining:.0f} 秒后再试。")
            return
        self.cooldown_manager.set_cooldown(user_id)

        # Extract text after command name
        args = self._get_args(event, "批量录入")
        if not args:
            args = self._get_args(event, "batch") or self._get_args(event, "bl")

        if not args or not args.strip():
            yield event.plain_result(
                "用法: 批量录入 后粘贴结算文本\n"
                "示例: 批量录入 【玩家：XXX ...】【登神之路+16】【觐见之梯+2】..."
            )
            return

        # Parse the text
        parsed_list, parse_err = self.ladder_service.parse_batch_scores(args.strip())
        if parse_err:
            yield event.plain_result(f"解析失败: {parse_err}")
            return

        # Execute batch update
        success_count, success_details, skipped = await self.ladder_service.batch_add_scores(
            group_id, parsed_list, user_id
        )

        # Build reply
        reply_parts = [f"批量录入完成: 成功 {success_count} 人"]
        if success_details:
            reply_parts.append("\n".join(success_details))
        if skipped:
            reply_parts.append(f"\n以下玩家不存在，已跳过: {', '.join(skipped)}")

        yield event.plain_result("\n".join(reply_parts))

    # === 录入玩家 ===

    @filter.command("录入玩家", alias={"register", "添加玩家"})
    async def cmd_register_player(self, event: AstrMessageEvent):
        """录入新玩家。格式: 录入玩家 <姓名> <信仰> <职业> <天梯分> <觐见分>"""
        group_id = self._get_group_id(event)
        user_id = str(event.get_sender_id())

        has_permission = await self.permission_service.check_score_permission(user_id)
        is_admin = self._is_plugin_admin(event)
        if not has_permission and not is_admin:
            yield event.plain_result("凡人也胆敢染指神明的权柄？")
            return

        args = self._get_args(event, "录入玩家")
        if not args:
            args = self._get_args(event, "register") or self._get_args(event, "添加玩家")

        parts = args.split()
        if len(parts) not in (3, 5):
            yield event.plain_result(
                f"用法: 录入玩家 <姓名> <信仰> <职业> [登神之路分] [觐见分]\n"
                f"示例: 录入玩家 张三 文明 战士 1000 100\n"
                f"      录入玩家 张三 文明 战士（使用默认分数）\n"
                f"可选职业: {'/'.join(VALID_CLASSES)}\n"
                f"可选信仰: {'/'.join(VALID_FAITHS)}"
            )
            return

        player_name, faith_name, class_name = parts[0], parts[1], parts[2]
        if len(parts) == 5:
            ladder_str, pilgrimage_str = parts[3], parts[4]
        else:
            ladder_str = str(self.config.get("init_ladder_score", 1000))
            pilgrimage_str = str(self.config.get("init_pilgrimage_score", 100))

        max_name_len = self.config.get("player_name_max_length", 20)
        if len(player_name) > max_name_len:
            yield event.plain_result(f"玩家名过长，最长 {max_name_len} 个字符。")
            return

        try:
            ladder_score = int(ladder_str)
            pilgrimage_score = int(pilgrimage_str)
        except ValueError:
            yield event.plain_result( "分数必须是整数。")
            return

        success, message = await self.ladder_service.register_player(
            group_id, player_name, faith_name, class_name,
            ladder_score, pilgrimage_score, user_id
        )
        yield event.plain_result( message)

    # === 设置职业（仅职业） ===

    @filter.command("设置职业", alias={"setclass", "改职业"})
    async def cmd_set_class(self, event: AstrMessageEvent):
        """修改玩家职业。格式: 设置职业 <玩家名> <职业>"""
        group_id = self._get_group_id(event)

        has_permission = await self.permission_service.check_score_permission(str(event.get_sender_id()))
        is_admin = self._is_plugin_admin(event)
        if not has_permission and not is_admin:
            yield event.plain_result("凡人也胆敢染指神明的权柄？")
            return

        args = self._get_args(event, "设置职业")
        if not args:
            args = self._get_args(event, "setclass") or self._get_args(event, "改职业")

        parts = args.split()
        if len(parts) != 2:
            yield event.plain_result(
                f"用法: 设置职业 <玩家名> <职业>\n"
                f"可选职业: {'/'.join(VALID_CLASSES)}"
            )
            return

        target_name, class_name = parts
        target_player = await self.db_manager.get_player_by_name(group_id, target_name)
        if not target_player:
            yield event.plain_result(f"{target_name}不属于这个宇宙")
            return

        success, message = await self.ladder_service.set_class(
            group_id, target_player.player_id, target_name, class_name
        )
        yield event.plain_result(message)

    # === 立誓（设置信仰） ===

    @filter.command("立誓", alias={"takeoath", "立约"})
    async def cmd_take_oath(self, event: AstrMessageEvent):
        """设置信仰。格式: 立誓 <玩家名> <信仰>"""
        group_id = self._get_group_id(event)

        has_permission = await self.permission_service.check_score_permission(str(event.get_sender_id()))
        is_admin = self._is_plugin_admin(event)
        if not has_permission and not is_admin:
            yield event.plain_result("凡人也胆敢染指神明的权柄？")
            return

        # Cooldown
        cooldown_seconds = self.config.get("ladder_cooldown_seconds", 600)
        user_id = str(event.get_sender_id())
        if not self.cooldown_manager.check_cooldown(user_id, cooldown_seconds):
            remaining = self.cooldown_manager.get_remaining(user_id, cooldown_seconds)
            yield event.plain_result(f"冷却中，请 {remaining:.0f} 秒后再试。")
            return
        self.cooldown_manager.set_cooldown(user_id)

        args = self._get_args(event, "立誓")
        if not args:
            args = self._get_args(event, "takeoath") or self._get_args(event, "立约")

        parts = args.split()
        if len(parts) != 2:
            yield event.plain_result(
                f"用法: 立誓 <玩家名> <信仰>\n"
                f"可选信仰: {'/'.join(VALID_FAITHS)}"
            )
            return

        target_name, faith_name = parts
        success, message = await self.ladder_service.set_faith(group_id, target_name, faith_name)
        yield event.plain_result(message)

    # === 弃誓 ===

    @filter.command("弃誓", alias={"abandoath"})
    async def cmd_abandon_oath(self, event: AstrMessageEvent):
        """标记弃誓者。格式: 弃誓 <玩家名> [新信仰]"""
        group_id = self._get_group_id(event)
        user_id = str(event.get_sender_id())

        has_permission = await self.permission_service.check_score_permission(user_id)
        is_admin = self._is_plugin_admin(event)
        if not has_permission and not is_admin:
            yield event.plain_result("凡人也胆敢染指神明的权柄？")
            return

        # Cooldown
        cooldown_seconds = self.config.get("ladder_cooldown_seconds", 600)
        if not self.cooldown_manager.check_cooldown(user_id, cooldown_seconds):
            remaining = self.cooldown_manager.get_remaining(user_id, cooldown_seconds)
            yield event.plain_result(f"冷却中，请 {remaining:.0f} 秒后再试。")
            return
        self.cooldown_manager.set_cooldown(user_id)

        args = self._get_args(event, "弃誓")
        if not args:
            args = self._get_args(event, "abandoath")

        parts = args.split()
        if not parts or len(parts) > 2:
            yield event.plain_result(
                f"用法: 弃誓 <玩家名> [新信仰]\n"
                f"示例: 弃誓 张三\n"
                f"      弃誓 张三 文明"
            )
            return

        target_name = parts[0]
        new_faith = parts[1] if len(parts) > 1 else None
        success, message = await self.ladder_service.abandon_oath(
            group_id, target_name, new_faith, dict(self.config)
        )
        yield event.plain_result(message)

    # === 天梯榜管理 ===

    @filter.command("天梯榜管理", alias={"ladderadmin", "榜管理"})
    async def cmd_admin(self, event: AstrMessageEvent):
        """管理员/白名单操作。格式: 天梯榜管理 <操作> [参数]"""
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
                f"全部重置/ resetall — 重置本群所有玩家积分 (管理员)\n"
                f"删除/ delete <玩家名> — 删除单个玩家 (白名单/管理员)\n"
                f"改名/ rename <旧名> <新名> — 改名 (白名单/管理员)\n"
                f"清空/ clear — 清空本群所有玩家和数据 (管理员)\n"
                f"清除弃誓/ clearoath <玩家名> — 清除弃誓者标记 (管理员)\n"
                f"\n"
                f"示例:\n"
                f"天梯榜管理 重置 张三\n"
                f"天梯榜管理 删除 张三\n"
                f"天梯榜管理 改名 张三 李四\n"
                f"天梯榜管理 清除弃誓 张三"
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
        }
        action = ACTION_MAP.get(action, action)

        # delete: whitelist or admin
        if action == "delete":
            has_permission = await self.permission_service.check_score_permission(user_id)
            if not has_permission and not is_admin:
                yield event.plain_result("仅供诸神使用")
                return
            if len(parts) < 2:
                yield event.plain_result("用法: 天梯榜管理 删除 <玩家名>")
                return
            target_name = parts[1]
            deleted = await self.db_manager.delete_player_by_name(group_id, target_name)
            if deleted:
                yield event.plain_result(f"已将玩家 {target_name} 数据在本宇宙删除。")
            else:
                yield event.plain_result(f"本宇宙未找到玩家: {target_name}")
            return

        # rename: whitelist or admin
        if action == "rename":
            has_permission = await self.permission_service.check_score_permission(user_id)
            if not has_permission and not is_admin:
                yield event.plain_result("仅供诸神使用")
                return
            if len(parts) < 3:
                yield event.plain_result("用法: 天梯榜管理 改名 <旧名> <新名>")
                return
            old_name, new_name = parts[1], parts[2]
            max_name_len = self.config.get("player_name_max_length", 20)
            if len(new_name) > max_name_len:
                yield event.plain_result(f"玩家名过长，最长 {max_name_len} 个字符。")
                return
            success, message = await self.db_manager.rename_player_by_name(group_id, old_name, new_name)
            yield event.plain_result(message)
            return

        # Other actions: admin only
        if not is_admin:
            yield event.plain_result("仅供诸神使用")
            return

        if action == "clearoath" and len(parts) >= 2:
            target_name = parts[1]
            target_player = await self.db_manager.get_player_by_name(group_id, target_name)
            if not target_player:
                yield event.plain_result(f"本宇宙未找到玩家: {target_name}")
                return
            await self.db_manager.clear_oathbreaker(group_id, target_player.player_id)
            yield event.plain_result(f"已清除 {target_name} 的弃誓者标记。")
            return

        if action == "reset" and len(parts) >= 2:
            target_name = parts[1]
            target_player = await self.db_manager.get_player_by_name(group_id, target_name)
            if not target_player:
                yield event.plain_result(f"本宇宙未找到玩家: {target_name}")
                return
            init_ladder = self.config.get("init_ladder_score", 1000)
            init_pilgrimage = self.config.get("init_pilgrimage_score", 100)
            await self.db_manager.update_scores(
                group_id, target_player.player_id,
                -target_player.ladder_score + init_ladder,
                -target_player.pilgrimage_score + init_pilgrimage,
                user_id, "管理员重置"
            )
            yield event.plain_result(f"已重置玩家 {target_name} 的积分（天梯: {init_ladder}, 觐见: {init_pilgrimage}）。")

        elif action == "resetall":
            init_ladder = self.config.get("init_ladder_score", 1000)
            init_pilgrimage = self.config.get("init_pilgrimage_score", 100)
            count = await self.db_manager.reset_all_scores(group_id)
            yield event.plain_result(f"已重置本群 {count} 名玩家的积分（天梯: {init_ladder}, 觐见: {init_pilgrimage}）。")

        elif action == "clear":
            count = await self.db_manager.delete_all_players(group_id)
            yield event.plain_result(f"已清空本群所有数据，共删除 {count} 名玩家。")

        else:
            yield event.plain_result(f"未知操作: {action}\n发送「天梯榜管理」查看所有可用操作。")

    # === 白名单 ===

    @filter.command("白名单", alias={"whitelist", "wl"})
    async def cmd_whitelist(self, event: AstrMessageEvent):
        """白名单管理。格式: 白名单 <add/remove/list> [类型] [ID]"""
        if not self._is_plugin_admin(event):
            yield event.plain_result( "权限不足: 仅管理员可管理白名单。")
            return

        user_id = str(event.get_sender_id())
        args = self._get_args(event, "白名单")
        if not args:
            args = self._get_args(event, "whitelist") or self._get_args(event, "wl")

        parts = args.split()
        if not parts:
            yield event.plain_result(
                f"用法: 白名单 <add/remove/list> [类型] [ID]\n"
                f"类型: user (用户) 或 group (群)"
            )
            return

        action = parts[0]
        if action == "list":
            text = await self.permission_service.get_whitelist_text()
            yield event.plain_result( text)
        elif action == "add" and len(parts) >= 3:
            _, message = await self.permission_service.add_to_whitelist(parts[1], parts[2], user_id)
            yield event.plain_result( message)
        elif action == "remove" and len(parts) >= 3:
            _, message = await self.permission_service.remove_from_whitelist(parts[1], parts[2])
            yield event.plain_result( message)
        else:
            yield event.plain_result(f"用法: 白名单 <add/remove/list> [类型] [ID]")

    # === 帮助 ===

    @filter.command("天梯榜帮助", alias={"ladderhelp", "帮助"})
    async def cmd_help(self, event: AstrMessageEvent):
        """显示帮助信息"""
        text = format_help(dict(self.config))
        yield event.plain_result(text)

    # === QQ 群管命令（委托到 QQAdminHandler） ===

    @filter.command("禁言")
    async def cmd_w_ban(self, event: AstrMessageEvent):
        async for result in self._qq_admin.handle_ban(event):
            yield result

    @filter.command("解禁")
    async def cmd_w_unban(self, event: AstrMessageEvent):
        async for result in self._qq_admin.handle_unban(event):
            yield result

    @filter.command("踢人")
    async def cmd_w_kick(self, event: AstrMessageEvent):
        async for result in self._qq_admin.handle_kick(event):
            yield result

    @filter.command("撤回")
    async def cmd_w_recall(self, event: AstrMessageEvent):
        async for result in self._qq_admin.handle_recall(event):
            yield result

    @filter.command("全员禁")
    async def cmd_w_mute_all(self, event: AstrMessageEvent):
        async for result in self._qq_admin.handle_mute_all(event):
            yield result

    @filter.command("全员解")
    async def cmd_w_unmute_all(self, event: AstrMessageEvent):
        async for result in self._qq_admin.handle_unmute_all(event):
            yield result

    # === 储物空间 ===

    @filter.command("查询储物空间")
    async def cmd_query_inventory(self, event: AstrMessageEvent):
        """查看玩家储物空间。格式: 查询储物空间 <玩家名>"""
        group_id = self._get_group_id(event)
        user_id = str(event.get_sender_id())

        has_permission = await self.permission_service.check_score_permission(user_id)
        is_admin = self._is_plugin_admin(event)
        if not has_permission and not is_admin:
            yield event.plain_result("权限不足。")
            return

        args = self._get_args(event, "查询储物空间")
        if not args or not args.strip():
            yield event.plain_result("用法: 查询储物空间 <玩家名>")
            return

        target_name = args.strip()
        text = await self.ladder_service.get_inventory_text(group_id, target_name)
        if text is None:
            yield event.plain_result(f"{target_name} 不存在。")
            return
        yield event.plain_result(text)

    def _parse_item_args(self, text: str) -> list:
        """解析道具参数。格式: 道具名*数量，空格分隔多个。
        返回: [(道具名, 数量), ...]
        道具名可包含（）括号，数量可选（默认1）。
        """
        items = []
        # 按空格分割，但需要保留括号内的内容
        # 策略: 先按空格分割，再检查每段是否有 *
        parts = text.strip().split()
        for part in parts:
            if '*' in part:
                # 以最后一个 * 分隔（道具名可能不包含 *，数量在最后）
                idx = part.rfind('*')
                name = part[:idx].strip()
                qty_str = part[idx+1:].strip()
                try:
                    qty = int(qty_str)
                except ValueError:
                    # 如果 * 后面不是数字，整段作为道具名
                    name = part
                    qty = 1
                if name:
                    items.append((name, qty))
            else:
                if part:
                    items.append((part, 1))
        return items

    @filter.command("赐予道具")
    async def cmd_give_item(self, event: AstrMessageEvent):
        """赐予道具。格式: 赐予道具 <玩家名> <道具1*数量> [道具2*数量] ..."""
        group_id = self._get_group_id(event)
        user_id = str(event.get_sender_id())

        has_permission = await self.permission_service.check_score_permission(user_id)
        is_admin = self._is_plugin_admin(event)
        if not has_permission and not is_admin:
            yield event.plain_result("权限不足。")
            return

        args = self._get_args(event, "赐予道具")
        if not args:
            yield event.plain_result("用法: 赐予道具 <玩家名> <道具*数量> ...\n示例: 赐予道具 张三 铁剑*2 生命药水*3")
            return

        parts = args.split(None, 1)  # 分割为玩家名 + 剩余
        if len(parts) < 2:
            yield event.plain_result("用法: 赐予道具 <玩家名> <道具*数量> ...")
            return

        player_name = parts[0]
        items = self._parse_item_args(parts[1])
        if not items:
            yield event.plain_result("未指定有效道具。格式: 道具名*数量")
            return

        success, message = await self.ladder_service.give_items(group_id, player_name, items)
        yield event.plain_result(message)

    @filter.command("收回道具")
    async def cmd_remove_item(self, event: AstrMessageEvent):
        """收回道具。格式: 收回道具 <玩家名> <道具1*数量> [道具2*数量] ..."""
        group_id = self._get_group_id(event)
        user_id = str(event.get_sender_id())

        has_permission = await self.permission_service.check_score_permission(user_id)
        is_admin = self._is_plugin_admin(event)
        if not has_permission and not is_admin:
            yield event.plain_result("权限不足。")
            return

        args = self._get_args(event, "收回道具")
        if not args:
            yield event.plain_result("用法: 收回道具 <玩家名> <道具*数量> ...\n示例: 收回道具 张三 铁剑*2 生命药水")
            return

        parts = args.split(None, 1)
        if len(parts) < 2:
            yield event.plain_result("用法: 收回道具 <玩家名> <道具*数量> ...")
            return

        player_name = parts[0]
        raw_items = self._parse_item_args(parts[1])
        if not raw_items:
            yield event.plain_result("未指定有效道具。")
            return

        # 对于收回，需要区分"有数量"和"无数量（全部收回）"
        # _parse_item_args 默认给1，但用户可能没指定数量
        # 重新解析: 没有 * 的道具 → quantity=None (全部收回)
        items = []
        for part in parts[1].strip().split():
            if '*' in part:
                idx = part.rfind('*')
                name = part[:idx].strip()
                qty_str = part[idx+1:].strip()
                try:
                    qty = int(qty_str)
                    items.append((name, qty))
                except ValueError:
                    items.append((part, None))
            else:
                if part:
                    items.append((part, None))  # None = 全部收回

        success, message = await self.ladder_service.take_items(group_id, player_name, items)
        yield event.plain_result(message)
