"""
信仰游戏天梯排行榜 - Faith Game Ladder Plugin for AstrBot
A dual-ladder ranking system with class/faith customization for group chats.
"""

import sys
import re
import time
from pathlib import Path
from typing import Optional, Tuple

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
    "2.1.0"
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
        self._pending_gifts_receive = None  # 当前待确认的赠送（接收阶段）
        self._pending_registrations = {}  # group_id -> 待注册状态（祷词验证）

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
            purge_expired_statuses=self.db_manager.purge_expired_statuses,
        )
        await self._scheduler.start()

        # 注册群成员变动监听（白名单自动同步）
        try:
            if hasattr(self.context, 'register_event_handler'):
                self.context.register_event_handler(self.on_group_member_change)
                logger.info("[AutoWhitelist] 群成员变动监听已注册")
        except Exception as e:
            logger.warning(f"[AutoWhitelist] 事件监听注册失败（可用'同步白名单'命令手动同步）: {e}")

        # 注册消息监听（祷词验证）
        try:
            if hasattr(self.context, 'register_event_handler'):
                self.context.register_event_handler(self._on_message_for_prayer)
                logger.info("[Prayer] 祷词验证消息监听已注册")
        except Exception as e:
            logger.warning(f"[Prayer] 消息监听注册失败: {e}")

        logger.info("FaithLadder plugin initialized")

    async def terminate(self):
        if self._scheduler:
            await self._scheduler.stop()
        await self.db_manager.close()
        logger.info("FaithLadder plugin terminated")

    # === 祷词验证处理 ===

    async def _check_prayer_verification(self, event: AstrMessageEvent) -> bool:
        """检查消息是否匹配待注册的祷词。返回是否已处理。"""
        group_id = self._get_group_id(event)
        pending = self._pending_registrations.get(group_id)
        if not pending:
            return False

        # 检查是否超时
        if pending["expire_at"] < time.time():
            del self._pending_registrations[group_id]
            return False

        # 检查发送者是否是目标成员
        sender_id = str(event.get_sender_id())
        if sender_id != pending["member_id"]:
            return False

        # 匹配祷词（忽略标点和空格，任意一个匹配即可）
        msg = self.normalize_prayer(event.message_str)
        prayers = pending.get("prayers", [])
        # 兼容旧格式（单个字符串）
        if not prayers and pending.get("prayer"):
            prayers = [pending["prayer"]]

        if not any(msg == self.normalize_prayer(p) for p in prayers):
            # 不匹配，静默忽略，等待下次消息
            return False

        # 匹配成功，完成注册
        target_name = pending["target_name"]
        faith_name = pending["faith"]
        class_name = pending["class_"]
        ladder_score = pending["ladder_score"]
        pilgrimage_score = pending["pilgrimage_score"]
        operator_id = pending["operator_id"]

        del self._pending_registrations[group_id]

        success, message = await self.ladder_service.register_player(
            group_id, target_name, faith_name, class_name,
            ladder_score, pilgrimage_score, operator_id
        )
        # 发送结果（作为新消息）
        try:
            from astrbot.api.message_components import Plain
            await self.context.send_message(
                event.message_obj.umo, [Plain(text=message)]
            )
        except Exception as e:
            logger.error(f"Failed to send registration result: {e}")
        return True

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

    # === 群名片解析与玩家识别 ===

    @staticmethod
    def normalize_prayer(text: str) -> str:
        """去除标点和空格，用于祷词匹配。"""
        return re.sub(
            r'[\s　，。！？、；：“”‘’（）【】《》…—\-\.,!?;:\'\"()\[\]<>]+',
            '', text
        )

    async def _resolve_name_from_card(self, card: str, group_id: str) -> Optional[str]:
        """从群名片解析玩家名，通过数据库匹配确认。
        尝试名片中的每个词，返回第一个匹配数据库玩家名的词。
        无匹配则返回 None。
        """
        card = card.strip()
        match = re.match(r'^【[^】]*】\s*(.*)', card)
        remaining = match.group(1).strip() if match else card.strip()

        # 提取所有词（跳过纯数字）
        words = [w for w in remaining.split() if not w.isdigit()]

        # 逐个匹配数据库
        for word in words:
            player = await self.db_manager.get_player_by_name(group_id, word)
            if player:
                return word

        return None

    async def _resolve_player_name(self, event: AstrMessageEvent) -> Optional[str]:
        """自动识别发送者自己的群名片中的玩家名。"""
        try:
            sender_id = str(event.get_sender_id())
            group_id = self._get_group_id(event)
            info = await event.bot.get_group_member_info(
                group_id=int(group_id), user_id=int(sender_id)
            )
            card = info.get("card", "") or info.get("nickname", "")
            if card:
                return await self._resolve_name_from_card(card, group_id)
        except Exception:
            pass
        return None

    async def _parse_target_name(self, event: AstrMessageEvent, args: str) -> Tuple[str, str]:
        """从命令参数中解析目标玩家名和剩余参数（诸神用）。
        返回 (target_name, rest_args)。
        """
        parts = args.strip().split(None, 1)
        rest = parts[1] if len(parts) > 1 else ""

        if not parts:
            return "", ""

        first = parts[0]
        # 检查是否是有效玩家名
        player = await self.db_manager.get_player_by_name(self._get_group_id(event), first)
        if player:
            return first, rest

        return "", args.strip()

    async def _resolve_target_or_self(
        self, event: AstrMessageEvent, args: str
    ) -> Tuple[Optional[str], str, Optional[str]]:
        """解析目标玩家，带权限控制。
        返回 (player_name, rest_args, error_message)。
        """
        user_id = str(event.get_sender_id())
        has_perm = await self.permission_service.check_score_permission(user_id)
        is_admin = self._is_plugin_admin(event)

        # 判断是否有明确指定目标
        has_explicit_target = bool(args.strip())

        if has_perm or is_admin:
            # 诸神/管理员：必须指定目标
            target, rest = await self._parse_target_name(event, args)
            if not target:
                return None, "", "请指定玩家名。"
            return target, rest, None
        else:
            # 非诸神：只能查自己
            if has_explicit_target:
                return None, "", "只能查询自己。"
            self_name = await self._resolve_player_name(event)
            if not self_name:
                return None, "", "无法识别你的身份，请确认群名片格式正确。"
            return self_name, args.strip(), None

    async def _find_member_by_name(
        self, event: AstrMessageEvent, player_name: str
    ) -> Optional[dict]:
        """查找群名片中包含指定玩家名的群成员。"""
        try:
            members = await event.bot.get_group_member_list(
                group_id=int(self._get_group_id(event))
            )
            for member in members:
                card = member.get("card", "") or member.get("nickname", "")
                if player_name in card:
                    return member
        except Exception:
            pass
        return None

    @filter.command("查询", alias={"query", "查看"})
    async def cmd_query(self, event: AstrMessageEvent):
        """查询玩家信息。格式: 查询（自动识别自己）或 查询 <玩家名>（诸神指定）"""
        group_id = self._get_group_id(event)
        user_id = str(event.get_sender_id())

        args = self._get_args(event, "查询")
        if not args:
            for alias in ("query", "查看"):
                args = self._get_args(event, alias)
                if args:
                    break

        target_name, _, error = await self._resolve_target_or_self(event, args or "")
        if error:
            yield event.plain_result(error)
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
        """录入新玩家。格式: 录入玩家 <姓名> <信仰> <职业> [天梯分] [觐见分]
        需要名片对应的玩家发送祷词确认才能完成注册。"""
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

        if faith_name not in VALID_FAITHS:
            yield event.plain_result(f"无效的信仰: {faith_name}。可选: {'/'.join(VALID_FAITHS)}")
            return

        if class_name not in VALID_CLASSES:
            yield event.plain_result(f"无效的职业: {class_name}。可选: {'/'.join(VALID_CLASSES)}")
            return

        try:
            ladder_score = int(ladder_str)
            pilgrimage_score = int(pilgrimage_str)
        except ValueError:
            yield event.plain_result("分数必须是整数。")
            return

        # 检查玩家是否已存在
        existing = await self.db_manager.get_player_by_name(group_id, player_name)
        if existing:
            yield event.plain_result(f"玩家 {player_name} 已存在。")
            return

        # 查找名片包含该玩家名的群成员
        target_member = await self._find_member_by_name(event, player_name)
        if not target_member:
            yield event.plain_result(f"未找到群名片包含「{player_name}」的群成员，无法进行祷词验证。")
            return

        # 获取祷词（支持数组格式，任意一个匹配即可）
        prayer_key = f"prayer_text_{faith_name}"
        prayers_raw = self.config.get(prayer_key, [])
        if isinstance(prayers_raw, list):
            prayers = [p for p in prayers_raw if p]
        else:
            prayers = [prayers_raw] if prayers_raw else []
        if not prayers:
            yield event.plain_result(f"未配置信仰 {faith_name} 的祷词，请联系管理员。")
            return

        # 存储待注册状态
        self._pending_registrations[group_id] = {
            "target_name": player_name,
            "faith": faith_name,
            "class_": class_name,
            "ladder_score": ladder_score,
            "pilgrimage_score": pilgrimage_score,
            "member_id": str(target_member["user_id"]),
            "operator_id": user_id,
            "expire_at": time.time() + 60,
            "prayers": prayers,
        }

        yield event.plain_result("请需要注册的玩家尽快发送祷词")

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

    @filter.command("设置精华")
    async def cmd_set_essence(self, event: AstrMessageEvent):
        async for result in self._qq_admin.handle_set_essence(event):
            yield result

    @filter.command("移除精华")
    async def cmd_remove_essence(self, event: AstrMessageEvent):
        async for result in self._qq_admin.handle_remove_essence(event):
            yield result

    # === 储物空间 ===

    @filter.command("查询储物空间")
    async def cmd_query_inventory(self, event: AstrMessageEvent):
        """查看玩家储物空间。格式: 查询储物空间（查自己）或 查询储物空间 <玩家名> ...（诸神批量）"""
        group_id = self._get_group_id(event)
        user_id = str(event.get_sender_id())

        has_permission = await self.permission_service.check_score_permission(user_id)
        is_admin = self._is_plugin_admin(event)

        args = self._get_args(event, "查询储物空间")
        args = args.strip() if args else ""

        if has_permission or is_admin:
            # 诸神/管理员：必须指定目标
            if not args:
                yield event.plain_result("用法: 查询储物空间 <玩家名> [玩家名2 ...]")
                return
            names = args.split()
        else:
            # 非诸神：只能查自己
            if args:
                yield event.plain_result("只能查询自己。")
                return
            self_name = await self._resolve_player_name(event)
            if not self_name:
                yield event.plain_result("无法识别你的身份，请确认群名片格式正确。")
                return
            names = [self_name]

        results = []
        not_found = []
        for name in names:
            text = await self.ladder_service.get_inventory_text(group_id, name)
            if text is None:
                not_found.append(name)
            else:
                results.append(text)

        parts = []
        if results:
            parts.append("\n\n".join(results))
        if not_found:
            parts.append(f"\n以下玩家不存在: {', '.join(not_found)}")

        yield event.plain_result("\n".join(parts) if parts else "未查询到任何玩家。")

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

    @filter.command("清除储物空间")
    async def cmd_clear_inventory(self, event: AstrMessageEvent):
        """清除储物空间。格式: 清除储物空间 <玩家名> [道具名|全部]
        清空全部道具需要加「全部」确认，清除指定道具不需要。"""
        group_id = self._get_group_id(event)
        user_id = str(event.get_sender_id())

        has_permission = await self.permission_service.check_score_permission(user_id)
        is_admin = self._is_plugin_admin(event)
        if not has_permission and not is_admin:
            yield event.plain_result("权限不足。")
            return

        args = self._get_args(event, "清除储物空间")
        if not args or not args.strip():
            yield event.plain_result(
                "用法: 清除储物空间 <玩家名> [道具名|全部]\n"
                "示例: 清除储物空间 Alice 全部        — 清空所有道具\n"
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

    # === 白名单自动同步 ===

    @filter.command("同步白名单")
    async def cmd_sync_whitelist(self, event: AstrMessageEvent):
        """同步指定群的当前成员到白名单。格式: 同步白名单"""
        if not self._is_plugin_admin(event):
            yield event.plain_result("权限不足：仅管理员可同步白名单。")
            return

        target_group = self.config.get("auto_whitelist_group", "")
        if not target_group:
            yield event.plain_result("请先在 WebUI 配置 auto_whitelist_group（自动白名单群号）。")
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
            success, _ = await self.permission_service.add_to_whitelist("user", uid, "sync")
            if success:
                added += 1

        yield event.plain_result(f"白名单同步完成: 新增 {added} 人（群 {target_group} 共 {len(members)} 名成员）")

    async def _handle_auto_whitelist(self, user_id: str, action: str):
        """处理白名单自动同步（加入/离开指定群）。"""
        target_group = self.config.get("auto_whitelist_group", "")
        if not target_group:
            return

        if action == "join":
            success, msg = await self.permission_service.add_to_whitelist("user", user_id, "auto")
            if success:
                logger.info(f"[AutoWhitelist] 自动添加白名单: {user_id}")
        elif action == "leave":
            success, msg = await self.permission_service.remove_from_whitelist("user", user_id)
            if success:
                logger.info(f"[AutoWhitelist] 自动移除白名单: {user_id}")

    async def on_group_member_change(self, event: AstrMessageEvent):
        """监听群成员变动事件，自动同步白名单。
        需要在 initialize() 中注册到事件总线。
        """
        try:
            # 检查是否为 aiocqhttp 的 notice 事件
            raw = getattr(event.message_obj, 'raw_message', None) or {}
            notice_type = raw.get('notice_type', '')
            group_id = str(raw.get('group_id', ''))
            user_id = str(raw.get('user_id', ''))

            target_group = self.config.get("auto_whitelist_group", "")
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

    async def _on_message_for_prayer(self, event: AstrMessageEvent):
        """监听所有消息，检查是否有待验证的祷词。"""
        try:
            await self._check_prayer_verification(event)
        except Exception as e:
            logger.error(f"[Prayer] 祷词验证处理失败: {e}")

    # === 状态 ===

    @filter.command("添加状态")
    async def cmd_add_status(self, event: AstrMessageEvent):
        """添加状态。格式: 添加状态 <玩家名> <状态名> <天数>"""
        group_id = self._get_group_id(event)
        user_id = str(event.get_sender_id())

        has_permission = await self.permission_service.check_score_permission(user_id)
        is_admin = self._is_plugin_admin(event)
        if not has_permission and not is_admin:
            yield event.plain_result("权限不足。")
            return

        args = self._get_args(event, "添加状态")
        if not args:
            yield event.plain_result("用法: 添加状态 <玩家名> <状态名> <天数>\n示例: 添加状态 繁荣 虚弱 3")
            return

        parts = args.split()
        if len(parts) < 3:
            yield event.plain_result("用法: 添加状态 <玩家名> <状态名> <天数>")
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

        success, message = await self.ladder_service.add_status(group_id, player_name, status_name, days)
        yield event.plain_result(message)

    @filter.command("移除状态")
    async def cmd_remove_status(self, event: AstrMessageEvent):
        """移除状态。格式: 移除状态 <玩家名> <状态名>"""
        group_id = self._get_group_id(event)
        user_id = str(event.get_sender_id())

        has_permission = await self.permission_service.check_score_permission(user_id)
        is_admin = self._is_plugin_admin(event)
        if not has_permission and not is_admin:
            yield event.plain_result("权限不足。")
            return

        args = self._get_args(event, "移除状态")
        if not args:
            yield event.plain_result("用法: 移除状态 <玩家名> <状态名>")
            return

        parts = args.split(None, 1)
        if len(parts) < 2:
            yield event.plain_result("用法: 移除状态 <玩家名> <状态名>")
            return

        player_name = parts[0]
        status_name = parts[1].strip()

        success, message = await self.ladder_service.remove_status(group_id, player_name, status_name)
        yield event.plain_result(message)

    @filter.command("清除状态")
    async def cmd_clear_status(self, event: AstrMessageEvent):
        """清除所有状态。格式: 清除状态 <玩家名>"""
        group_id = self._get_group_id(event)
        user_id = str(event.get_sender_id())

        has_permission = await self.permission_service.check_score_permission(user_id)
        is_admin = self._is_plugin_admin(event)
        if not has_permission and not is_admin:
            yield event.plain_result("权限不足。")
            return

        args = self._get_args(event, "清除状态")
        if not args or not args.strip():
            yield event.plain_result("用法: 清除状态 <玩家名>")
            return

        player_name = args.strip()
        success, message = await self.ladder_service.clear_statuses(group_id, player_name)
        yield event.plain_result(message)

    # === 赠送道具 ===

    @filter.command("赠送道具")
    async def cmd_gift_item(self, event: AstrMessageEvent):
        """赠送道具。格式: 赠送道具 <接收方名> <道具*数量>
        发送方自动为命令发送者（从群名片识别），直接扣除道具，等待接收方接受/拒绝。"""
        group_id = self._get_group_id(event)

        # 发送方 = 自己（从群名片识别）
        sender_name = await self._resolve_player_name(event)
        if not sender_name:
            yield event.plain_result("无法识别你的身份，请确认群名片格式正确。")
            return

        args = self._get_args(event, "赠送道具")
        if not args:
            yield event.plain_result("用法: 赠送道具 <接收方名> <道具*数量>\n示例: 赠送道具 Bob 铁剑*3")
            return

        parts = args.split(None, 1)
        if len(parts) < 2:
            yield event.plain_result("用法: 赠送道具 <接收方名> <道具*数量>")
            return

        receiver_name, item_args = parts[0], parts[1].strip()

        # 解析道具（只支持一种）
        items = self._parse_item_args(item_args)
        if not items:
            yield event.plain_result("未指定有效道具。格式: 道具名*数量")
            return

        item_raw, quantity = items[0]

        # 查找发送方和接收方
        sender_player = await self.db_manager.get_player_by_name(group_id, sender_name)
        if not sender_player:
            yield event.plain_result(f"玩家 {sender_name} 不存在。")
            return

        receiver_player = await self.db_manager.get_player_by_name(group_id, receiver_name)
        if not receiver_player:
            yield event.plain_result(f"玩家 {receiver_name} 不存在。")
            return

        if sender_player.player_id == receiver_player.player_id:
            yield event.plain_result("不能赠送给自己。")
            return

        # 直接扣除发送方道具
        success, msg, base_name, grade = await self.ladder_service.deduct_item(
            group_id, sender_player.player_id, sender_name, item_raw, quantity
        )
        if not success:
            yield event.plain_result(msg)
            return

        # 存入接收方待确认
        self._pending_gifts_receive = {
            "group_id": group_id,
            "sender_id": sender_player.player_id,
            "sender_name": sender_name,
            "receiver_id": receiver_player.player_id,
            "receiver_name": receiver_name,
            "item_name": base_name,
            "grade": grade,
            "quantity": quantity,
        }

        from astrbot_plugin_faith_ladder.message_formatter import format_gift_request
        notification = format_gift_request(
            sender_name, receiver_name, base_name, grade, quantity
        )
        yield event.plain_result(
            f"已从 {sender_name} 扣除，等待 {receiver_name} 接受。\n\n{notification}"
        )

    @filter.command("接受道具")
    async def cmd_accept_gift(self, event: AstrMessageEvent):
        """接收方接受赠送（白名单权限），无需参数。"""
        user_id = str(event.get_sender_id())
        has_permission = await self.permission_service.check_score_permission(user_id)
        is_admin = self._is_plugin_admin(event)
        if not has_permission and not is_admin:
            yield event.plain_result("权限不足：只有白名单用户才能接受赠送。")
            return

        gift = self._pending_gifts_receive
        if not gift:
            yield event.plain_result("没有待接受的赠送。")
            return

        from astrbot_plugin_faith_ladder.ladder_service import format_item_display
        success, msg = await self.ladder_service.receive_item(
            gift["group_id"], gift["receiver_id"], gift["receiver_name"],
            gift["item_name"], gift["quantity"], grade=gift.get("grade")
        )
        self._pending_gifts_receive = None

        if success:
            display = format_item_display(gift["item_name"], gift.get("grade"), gift["quantity"])
            yield event.plain_result(
                f"已接受 {gift['sender_name']} 赠送的 {display}"
            )
        else:
            yield event.plain_result(f"接受失败: {msg}")

    @filter.command("拒绝道具")
    async def cmd_reject_gift(self, event: AstrMessageEvent):
        """接收方拒绝赠送（白名单权限），无需参数。"""
        user_id = str(event.get_sender_id())
        has_permission = await self.permission_service.check_score_permission(user_id)
        is_admin = self._is_plugin_admin(event)
        if not has_permission and not is_admin:
            yield event.plain_result("权限不足：只有白名单用户才能拒绝赠送。")
            return

        gift = self._pending_gifts_receive
        if not gift:
            yield event.plain_result("没有待拒绝的赠送。")
            return

        from astrbot_plugin_faith_ladder.ladder_service import format_item_display
        # 退回赠送方道具
        await self.ladder_service.receive_item(
            gift["group_id"], gift["sender_id"], gift["sender_name"],
            gift["item_name"], gift["quantity"], grade=gift.get("grade")
        )
        self._pending_gifts_receive = None

        display = format_item_display(gift["item_name"], gift.get("grade"), gift["quantity"])
        yield event.plain_result(
            f"已拒绝 {gift['sender_name']} 的赠送，{display} 已退回"
        )
