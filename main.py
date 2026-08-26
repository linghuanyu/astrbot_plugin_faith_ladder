"""
信仰游戏天梯排行榜 - Faith Game Ladder Plugin for AstrBot
A dual-ladder ranking system with class/faith customization for group chats.
"""

import sys
import re
import json
import asyncio
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
from astrbot_plugin_faith_ladder.message_formatter import format_help, format_prayer_trigger
from astrbot_plugin_faith_ladder.models import VALID_CLASSES, VALID_FAITHS, VALID_PATHS, Player
# from astrbot_plugin_faith_ladder.image_renderer import ImageRenderer  # 暂时禁用图片渲染
from astrbot_plugin_faith_ladder.qq_admin_handle import QQAdminHandler

# 预编译正则（祷词触发用）
_PRAYER_NORMALIZE_RE = re.compile(r'[^\w]')
_PRAYER_CHINESE_RE = re.compile(r'[一-鿿]')

# 预编译正则（通用）
_CQ_CODE_RE = re.compile(r'\[CQ:[^\]]+\]')
_AT_MENTION_RE = re.compile(r'@\S+')
_CARD_BRACKET_RE = re.compile(r'^【[^】]*】\s*(.*)')
_CARD_CONTENT_RE = re.compile(r'^【([^】]*)】\s*(.*)')


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
        # self.image_renderer = ImageRenderer(self)  # 暂时禁用图片渲染

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
        self._pending_gifts_receive = {}  # (group_id, receiver_id) -> gift_dict（内存缓存）

        # 祷词触发缓存
        self._prayer_cache = {}  # {normalized_prayer: faith}
        self._command_prefixes = set()
        self._build_prayer_cache()

        # 加载具体职业映射
        self._specific_classes = {}  # specific_class_name -> (faith, basic_class)
        self._load_specific_classes()

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

    def _load_specific_classes(self):
        """加载具体职业映射文件，构建 具体职业 -> (信仰, 命途, 普通职业) 的反向映射。"""
        from astrbot_plugin_faith_ladder.models import FAITH_TO_PATH
        json_path = _plugin_dir / "specific_classes.json"
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for faith, classes in data.items():
                path = FAITH_TO_PATH.get(faith)
                for basic_class, specific_name in classes.items():
                    if basic_class == "祷词":
                        continue  # 跳过祷词
                    self._specific_classes[specific_name] = (faith, path, basic_class)
            logger.info(f"[SpecificClasses] 加载 {len(self._specific_classes)} 个具体职业映射")
        except Exception as e:
            logger.warning(f"[SpecificClasses] 加载具体职业映射失败: {e}")

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

        self._scheduler = SchedulerService(
            data_dir=self.data_dir,
            get_config=lambda: dict(self.config),
            purge_score_history=self.db_manager.purge_old_score_history,
            purge_expired_statuses=self.db_manager.purge_expired_statuses,
            cleanup_expired_gifts=self.ladder_service.cleanup_expired_gifts,
            notify_gift_timeout=send_to_group,
        )
        await self._scheduler.start()

        # QQ 绑定自动迁移：延迟到首次事件时触发（需要 bot API 引用）
        self._qq_migration_attempted = False
        self._qq_migration_lock = asyncio.Lock()

        # 注册群成员变动监听（白名单自动同步）
        try:
            if hasattr(self.context, 'register_event_handler'):
                self.context.register_event_handler(self.on_group_member_change)
                logger.info("[AutoWhitelist] 群成员变动监听已注册")
        except Exception as e:
            logger.warning(f"[AutoWhitelist] 事件监听注册失败（可用'同步白名单'命令手动同步）: {e}")

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

    async def _check_perm(self, event: AstrMessageEvent) -> bool:
        """检查诸神/管理员权限。返回 True 表示有权限，False 表示无权限。"""
        user_id = str(event.get_sender_id())
        has_permission = await self.permission_service.check_score_permission(user_id)
        is_admin = self._is_plugin_admin(event)
        return has_permission or is_admin

    async def _resolve_self_player(self, event: AstrMessageEvent) -> Optional[Player]:
        """通过发送者的 QQ 号查找绑定的玩家记录（强鉴权，不可伪造）。"""
        group_id = self._get_group_id(event)
        qq_id = str(event.get_sender_id())
        return await self.db_manager.get_player_by_qq(group_id, qq_id)

    async def _resolve_self_player_lenient(self, event: AstrMessageEvent) -> Optional[Player]:
        """优先 QQ 绑定查找；失败则回退名片识别（兼容未绑定的老玩家）。
        仅用于只读命令（查询/查储物空间）。"""
        player = await self._resolve_self_player(event)
        if player:
            return player
        name = await self._resolve_player_name(event)
        if not name:
            return None
        group_id = self._get_group_id(event)
        return await self.db_manager.get_player_by_name(group_id, name)

    async def _maybe_trigger_qq_migration(self, event: AstrMessageEvent):
        """首次事件时触发后台 QQ 绑定迁移；只运行一次。"""
        if self._qq_migration_attempted:
            return
        async with self._qq_migration_lock:
            if self._qq_migration_attempted:
                return
            self._qq_migration_attempted = True
        try:
            bot = getattr(event, 'bot', None)
            if bot:
                asyncio.create_task(self._do_auto_migrate_qq(bot))
            else:
                logger.warning("[QQMigration] 无法获取 bot 引用，迁移跳过")
        except Exception as e:
            logger.error(f"[QQMigration] 触发失败: {e}")

    async def _do_auto_migrate_qq(self, bot):
        """后台迁移：扫描各活跃群，为未绑定 QQ 的玩家找到唯一匹配的名片 → 自动绑定。"""
        try:
            groups = await self.db_manager.get_active_groups()
            total_bound = 0
            for group_id in groups:
                try:
                    members = await bot.get_group_member_list(group_id=int(group_id))
                except Exception as e:
                    logger.warning(f"[QQMigration] 拉取群 {group_id} 成员失败（跳过）: {e}")
                    continue

                unbound = await self.db_manager.list_unbound_players(group_id)
                if not unbound:
                    continue

                for player in unbound:
                    matches = []
                    for m in members:
                        card = m.get("card") or m.get("nickname") or ""
                        if not card:
                            continue
                        words = self._extract_card_words(card)
                        if player.player_name in words:
                            matches.append(m)
                    if len(matches) == 1:
                        qq = str(matches[0].get("user_id"))
                        ok = await self.db_manager.set_player_qq(group_id, player.player_id, qq)
                        if ok:
                            total_bound += 1
                            logger.info(
                                f"[QQMigration] 自动绑定：群 {group_id} 玩家 {player.player_name} ↔ QQ {qq}"
                            )
                        else:
                            logger.info(
                                f"[QQMigration] 绑定冲突：群 {group_id} 玩家 {player.player_name} ↔ QQ {qq}"
                            )
                    elif len(matches) > 1:
                        logger.info(
                            f"[QQMigration] 玩家 {player.player_name} 在群 {group_id} 匹配到多个名片，跳过"
                        )
            if total_bound > 0:
                logger.info(f"[QQMigration] 自动绑定完成：共绑定 {total_bound} 个玩家")
        except Exception as e:
            logger.error(f"[QQMigration] 自动迁移失败: {e}")

    async def _resolve_player_by_at(
        self, group_id: str, at_user_id: Optional[str], event: AstrMessageEvent
    ) -> Tuple[Optional[Player], Optional[str]]:
        """通过 @用户 → 名片词 → DB 匹配，解析出目标玩家。
        返回 (player, error_message)。成功时 error_message=None；
        失败或歧义时 player=None 且 error_message 已给出。
        """
        if not at_user_id:
            return None, None
        try:
            info = await event.bot.get_group_member_info(
                group_id=int(group_id), user_id=int(at_user_id)
            )
            card = info.get("card") or info.get("nickname") or ""
        except Exception:
            return None, None
        if not card:
            return None, None
        words = self._extract_card_words(card)
        matched = []
        for w in words:
            p = await self.db_manager.get_player_by_name(group_id, w)
            if p:
                matched.append(p)
        if len(matched) == 1:
            return matched[0], None
        if len(matched) > 1:
            names = "、".join(p.player_name for p in matched)
            return None, f"名片匹配到多个玩家（{names}），请直接指定玩家名。"
        return None, None

    async def _get_valid_pending_gift(self, group_id: str, receiver_id: str,
                                       max_age_seconds: int = 240) -> Optional[dict]:
        """获取有效的待处理赠送（未超时）。超时则自动退回发送方并返回 None。"""
        gift_key = (group_id, receiver_id)

        # 从 DB 获取（含 created_at）
        db_gift = await self.db_manager.get_pending_gift(group_id, receiver_id)
        if not db_gift:
            self._pending_gifts_receive.pop(gift_key, None)
            return None

        # 检查超时
        from datetime import datetime, timezone
        created_at = datetime.strptime(db_gift["created_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - created_at).total_seconds() > max_age_seconds:
            # 超时，退回发送方
            items = db_gift["items"]
            await self.ladder_service.receive_item(
                group_id, db_gift["sender_id"], db_gift["sender_name"],
                items["item_name"], items["quantity"], grade=items.get("grade")
            )
            self._pending_gifts_receive.pop(gift_key, None)
            await self.db_manager.delete_pending_gift(group_id, receiver_id)
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
        self._pending_gifts_receive[gift_key] = gift
        return gift

    # === 图片渲染已暂时禁用 ===
    # async def _render_and_send(
    #     self,
    #     event: AstrMessageEvent,
    #     group_id: str,
    #     is_ladder: bool,
    #     render_func,
    #     get_text_func,
    #     limit: int
    # ):
    #     """Render image and send, with fallback to text on failure."""
    #     players = await render_func(group_id, limit)
    #     if not players:
    #         yield event.plain_result("暂无排名数据。")
    #         return
    #
    #     image_format = self.config.get("image_format", "PNG")
    #     image_quality = self.config.get("image_quality", 90)
    #
    #     # Try image rendering (returns bytes)
    #     if is_ladder:
    #         image_bytes = await self.image_renderer.render_leaderboard_image(
    #             players, limit, image_format=image_format, quality=image_quality
    #         )
    #     else:
    #         image_bytes = await self.image_renderer.render_pilgrimage_image(
    #             players, limit, image_format=image_format, quality=image_quality
    #         )
    #
    #     if image_bytes:
    #         from astrbot.api.message_components import Image
    #         yield event.chain_result([Image.fromBytes(image_bytes)])
    #     else:
    #         # Fallback to text
    #         text = await get_text_func(group_id, limit)
    #         yield event.plain_result(text + "\n[图片渲染失败，已降级为文本]")

    # === 排行榜 ===

    @filter.command("天梯榜", alias={"ladder", "ranking", "排行榜"})
    async def cmd_ladder(self, event: AstrMessageEvent):
        """显示天梯排行榜（需要诸神权限）"""
        user_id = str(event.get_sender_id())

        # Permission check
        if not await self._check_perm(event):
            yield event.plain_result("权限不足：区区凡人")
            return

        # Cooldown check
        cooldown_seconds = self.config.get("ladder_cooldown_seconds", 600)
        cd_key = f"{user_id}:ladder"
        if not self.cooldown_manager.check_cooldown(cd_key, cooldown_seconds):
            remaining = self.cooldown_manager.get_remaining(cd_key, cooldown_seconds)
            yield event.plain_result(f"排行榜冷却中，请 {remaining:.0f} 秒后再试。")
            return
        self.cooldown_manager.set_cooldown(cd_key)

        group_id = self._get_group_id(event)
        limit = self.config.get("ladder_display_limit", 10)
        text = await self.ladder_service.get_leaderboard_text(group_id, limit)
        yield event.plain_result(text)

    # === 觐见榜 ===

    @filter.command("觐见榜", alias={"pilgrimage", "觐见"})
    async def cmd_pilgrimage(self, event: AstrMessageEvent):
        """显示觐见之梯排行榜（需要诸神权限）"""
        user_id = str(event.get_sender_id())

        # Permission check
        if not await self._check_perm(event):
            yield event.plain_result("权限不足：区区凡人")
            return

        # Cooldown check
        cooldown_seconds = self.config.get("ladder_cooldown_seconds", 600)
        cd_key = f"{user_id}:pilgrimage"
        if not self.cooldown_manager.check_cooldown(cd_key, cooldown_seconds):
            remaining = self.cooldown_manager.get_remaining(cd_key, cooldown_seconds)
            yield event.plain_result(f"排行榜冷却中，请 {remaining:.0f} 秒后再试。")
            return
        self.cooldown_manager.set_cooldown(cd_key)

        group_id = self._get_group_id(event)
        limit = self.config.get("ladder_display_limit", 10)
        text = await self.ladder_service.get_pilgrimage_leaderboard_text(group_id, limit)
        yield event.plain_result(text)

    # === 输出模式切换（已暂时禁用） ===

    # @filter.command("输出模式", alias={"outputmode", "模式切换"})
    # async def cmd_output_mode(self, event: AstrMessageEvent):
    #     """切换输出模式（仅管理员）。格式: 输出模式 <text|image>"""
    #     if not self._is_plugin_admin(event):
    #         yield event.plain_result("权限不足：仅管理员可切换输出模式。")
    #         return
    #
    #     group_id = self._get_group_id(event)
    #
    #     # Get argument
    #     args = self._get_args(event, "输出模式")
    #     if not args:
    #         args = self._get_args(event, "outputmode") or self._get_args(event, "模式切换")
    #
    #     if not args or args not in ("text", "image"):
    #         current_mode = await self.ladder_service.get_effective_output_mode(
    #             group_id, self.config.get("output_mode", "text")
    #         )
    #         yield event.plain_result(
    #             f"当前群输出模式: {current_mode}\n"
    #             f"全局默认模式: {self.config.get('output_mode', 'text')}\n\n"
    #             f"用法：输出模式 <text|image>\n"
    #             f"  text  - 纯文本输出\n"
    #             f"  image - 图片输出"
    #         )
    #         return
    #
    #     # Store per-group mode in DB
    #     await self.db_manager.set_group_output_mode(group_id, args)
    #     yield event.plain_result(f"本群输出模式已切换为: {args}")

    # === 群名片解析与玩家识别 ===


    async def _resolve_name_from_card(self, card: str, group_id: str) -> Optional[str]:
        """从群名片解析玩家名，通过数据库匹配确认。
        尝试名片中的每个词，返回第一个匹配数据库玩家名的词。
        无匹配则返回 None。
        """
        words = self._extract_card_words(card)

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

        if has_perm or is_admin:
            # 诸神/管理员：必须指定目标
            target, rest = await self._parse_target_name(event, args)
            if not target:
                return None, "", "请指定玩家名。"
            return target, rest, None
        else:
            # 非诸神：只能查自己，无视后面的参数（优先 QQ 绑定，回退名片识别）
            self_player = await self._resolve_self_player_lenient(event)
            if not self_player:
                return None, "", "无法识别你的身份，请先让诸神为你「绑定QQ」或确认群名片格式正确。"
            return self_player.player_name, "", None

    async def _find_member_by_name(
        self, event: AstrMessageEvent, player_name: str
    ) -> Optional[dict]:
        """查找群名片中包含指定玩家名的群成员。
        使用词级匹配：解析名片后逐词精确比较，避免子串误匹配。
        如果匹配到多个成员，返回 None。
        """
        try:
            members = await event.bot.get_group_member_list(
                group_id=int(self._get_group_id(event))
            )
            matches = []
            for member in members:
                card = member.get("card", "") or member.get("nickname", "")
                # 用词级匹配：解析名片后逐词比较
                words = self._extract_card_words(card)
                if player_name in words:
                    matches.append(member)
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                logger.warning(
                    f"[FindMember] 玩家名 {player_name} 匹配到 {len(matches)} 个成员，无法确定"
                )
        except Exception:
            pass
        return None

    def _extract_card_words(self, card: str) -> list:
        """从群名片中提取所有非纯数字的词（用于匹配）。"""
        card = card.strip()
        match = _CARD_BRACKET_RE.match(card)
        remaining = match.group(1).strip() if match else card.strip()
        return [w for w in remaining.split() if not w.isdigit()]

    async def _get_at_user_id(self, event: AstrMessageEvent) -> Optional[str]:
        """获取消息中第一个 @ 的用户 ID（排除机器人自身）。"""
        try:
            from astrbot.core.message.components import At
            for seg in event.get_messages():
                if isinstance(seg, At) and str(seg.qq) != str(event.get_self_id()):
                    return str(seg.qq)
        except Exception:
            pass
        return None

    def _parse_card_info(self, card: str) -> dict:
        """从群名片中提取命途、职业、玩家名。
        返回 {"faith": str|None, "class_": str|None, "player_name": str|None}
        注意：faith 字段存储的是命途（如"生命"），不是具体信仰（如"繁荣"）
        规则：
        1. 【标签】若在 VALID_FAITHS（16个具体信仰）中 → 映射到命途
        2. 找到具体职业或普通职业（支持职业名后紧跟数字的情况，如"魔术师1218"）
        3. 剩余非关键词拼接为玩家名
        """
        from astrbot_plugin_faith_ladder.models import FAITH_TO_PATH, VALID_FAITHS as SPECIFIC_FAITHS
        result = {"faith": None, "class_": None, "player_name": None}
        card = card.strip()

        # 1. 提取标签
        tag = None
        match = _CARD_CONTENT_RE.match(card)
        if match:
            tag = match.group(1).strip()
            remaining = match.group(2).strip()
        else:
            remaining = card.strip()

        # 标签是具体信仰（如"繁荣"），映射到命途（如"生命"）
        if tag and tag in SPECIFIC_FAITHS:
            result["faith"] = FAITH_TO_PATH.get(tag)

        # 2. 提取非数字词
        words = [w for w in remaining.split() if not w.isdigit()]

        # 3. 找职业（支持职业名后紧跟数字/字母的情况）
        # 按职业名长度降序排序，确保长的优先匹配
        sorted_classes = sorted(self._specific_classes.items(), key=lambda x: len(x[0]), reverse=True)

        class_word = None
        name_parts = []  # 存储玩家名的部分

        for word in words:
            # 检查是否是具体职业（完全匹配或以具体职业开头）
            found_specific = False
            for specific_name, (specific_faith, specific_path, specific_class) in sorted_classes:
                if word == specific_name:
                    # 完全匹配
                    result["class_"] = specific_class
                    if result["faith"] is None:
                        result["faith"] = specific_path
                    class_word = word
                    found_specific = True
                    break
                elif word.startswith(specific_name) and len(word) > len(specific_name):
                    # 以具体职业开头（如"魔术师1218"）
                    result["class_"] = specific_class
                    if result["faith"] is None:
                        result["faith"] = specific_path
                    class_word = word
                    # 剩余部分加入玩家名
                    remainder = word[len(specific_name):]
                    if remainder and not remainder.isdigit():
                        name_parts.append(remainder)
                    found_specific = True
                    break

            if found_specific:
                continue

            # 检查是否是普通职业
            if word in VALID_CLASSES:
                result["class_"] = word
                class_word = word
                continue

            # 其他非关键词加入玩家名
            if word not in SPECIFIC_FAITHS and word not in VALID_PATHS:
                name_parts.append(word)

        if name_parts:
            result["player_name"] = "".join(name_parts)

        return result

    @filter.command("查询", alias={"query", "查看"})
    async def cmd_query(self, event: AstrMessageEvent):
        """查询玩家信息。格式: 查询（自动识别自己）或 查询 <玩家名>（诸神指定）或 查询 @用户（诸神专用）或 查询 <玩家名1> <玩家名2> ...（诸神批量查询）"""
        group_id = self._get_group_id(event)
        user_id = str(event.get_sender_id())
        await self._maybe_trigger_qq_migration(event)

        args = self._get_args(event, "查询")
        if not args:
            for alias in ("query", "查看"):
                args = self._get_args(event, alias)
                if args:
                    break

        # 先检测权限
        has_perm = await self.permission_service.check_score_permission(user_id)
        is_admin = self._is_plugin_admin(event)
        target_name = None
        target_names = None  # 批量查询用

        if has_perm or is_admin:
            # 诸神/管理员：处理 @ 或玩家名参数
            at_user_id = await self._get_at_user_id(event)
            if at_user_id:
                # @ 查询：从名片识别玩家
                try:
                    member_info = await event.bot.get_group_member_info(
                        group_id=int(group_id), user_id=int(at_user_id)
                    )
                    card = member_info.get("card") or member_info.get("nickname") or ""
                    if card:
                        target_name = await self._resolve_name_from_card(card, group_id)
                    if not target_name:
                        yield event.plain_result("无法从该用户的名片识别到玩家。")
                        return
                except Exception:
                    yield event.plain_result("获取用户信息失败。")
                    return
            else:
                # 解析玩家名参数
                args_str = args.strip() if args else ""
                # 检测是否为批量查询（多个空格分隔的名字）
                name_parts = args_str.split() if args_str else []
                if len(name_parts) > 1:
                    # 批量查询模式
                    target_names = name_parts
                else:
                    # 单查询模式
                    target_name, _, error = await self._resolve_target_or_self(event, args_str)
                    if error:
                        yield event.plain_result(error)
                        return
        else:
            # 非诸神：无视所有参数，直接查自己（优先 QQ 绑定，回退名片识别）
            self_player = await self._resolve_self_player_lenient(event)
            if not self_player:
                yield event.plain_result(
                    "无法识别你的身份，请先让诸神为你「绑定QQ」或确认群名片格式正确。"
                )
                return
            target_name = self_player.player_name

        cooldown_seconds = self.config.get("query_cooldown_seconds", 5)
        cd_key = f"{user_id}:query"
        if not self.cooldown_manager.check_cooldown(cd_key, cooldown_seconds):
            remaining = self.cooldown_manager.get_remaining(cd_key, cooldown_seconds)
            yield event.plain_result(f"查询冷却中，请 {remaining:.0f} 秒后再试。")
            return
        self.cooldown_manager.set_cooldown(cd_key)

        init_ladder = self.config.get("init_ladder_score", 1000)
        init_pilgrimage = self.config.get("init_pilgrimage_score", 100)

        # 批量查询模式
        if target_names:
            cards_text, not_found = await self.ladder_service.get_player_cards_by_names(
                group_id, target_names,
                init_ladder=init_ladder,
                init_pilgrimage=init_pilgrimage
            )
            if not cards_text and not_found:
                yield event.plain_result(f" {', '.join(not_found)} 不属于这个宇宙")
                return
            result_parts = []
            if cards_text:
                result_parts.append(cards_text)
            if not_found:
                result_parts.append(f"\n以下玩家不存在: {', '.join(not_found)}")
            yield event.plain_result("\n".join(result_parts))
            return

        # 单查询模式
        text = await self.ladder_service.get_player_card_by_name(
            group_id, target_name,
            init_ladder=init_ladder,
            init_pilgrimage=init_pilgrimage
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

        if not await self._check_perm(event):
            yield event.plain_result("凡人也胆敢染指神明的权柄？")
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

        max_name_len = self.config.get("player_name_max_length", 20)
        if len(target_name) > max_name_len:
            yield event.plain_result(f"玩家名过长，最长 {max_name_len} 个字符。")
            return

        try:
            ladder_delta = int(ladder_str)
            pilgrimage_delta = int(pilgrimage_str)
        except ValueError:
            yield event.plain_result("分数必须是整数。示例：100 50 或 -20 10")
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

        if not await self._check_perm(event):
            yield event.plain_result("凡人也胆敢染指神明的权柄？")
            return

        # Cooldown check
        cooldown_seconds = self.config.get("ladder_cooldown_seconds", 600)
        cd_key = f"{user_id}:batch"
        if not self.cooldown_manager.check_cooldown(cd_key, cooldown_seconds):
            remaining = self.cooldown_manager.get_remaining(cd_key, cooldown_seconds)
            yield event.plain_result(f"批量录入冷却中，请 {remaining:.0f} 秒后再试。")
            return
        self.cooldown_manager.set_cooldown(cd_key)

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

        # 自动将批量录入消息设置为精华消息
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

    # === 录入玩家 ===

    @filter.command("录入玩家", alias={"register", "添加玩家"})
    async def cmd_register_player(self, event: AstrMessageEvent):
        """录入新玩家。格式:
        录入玩家 @用户 [姓名] [信仰] [职业] [登神之路分] [觐见分]
          - @用户时自动从名片提取信仰/职业/姓名，显式参数可覆盖
        录入玩家 <姓名> <信仰> <职业> [登神之路分] [觐见分]
          - 传统方式，手动指定所有参数
        """
        group_id = self._get_group_id(event)
        user_id = str(event.get_sender_id())

        if not await self._check_perm(event):
            yield event.plain_result("凡人也胆敢染指神明的权柄？")
            return

        args = self._get_args(event, "录入玩家")
        if not args:
            args = self._get_args(event, "register") or self._get_args(event, "添加玩家")

        # 检查是否有 @ 用户
        at_user_id = await self._get_at_user_id(event)
        auto_faith = None
        auto_class = None
        auto_name = None
        target_member = None

        if at_user_id:
            # 获取 @ 用户的群名片
            try:
                member_info = await event.bot.get_group_member_info(
                    group_id=int(group_id), user_id=int(at_user_id)
                )
                card = member_info.get("card") or member_info.get("nickname") or ""
                if card:
                    parsed = self._parse_card_info(card)
                    auto_faith = parsed["faith"]
                    auto_class = parsed["class_"]
                    auto_name = parsed["player_name"]
                target_member = member_info
            except Exception:
                pass

        # 从参数文本中提取显式值
        # 当使用 @ 且名片解析成功时，不再从 args 提取参数（避免名片内容被误识别为显式参数）
        explicit_name = None
        explicit_faith = None
        explicit_class = None
        scores = []

        if not (at_user_id and auto_name):
            # 只有非 @ 模式，或名片解析失败时，才从 args 提取显式参数
            clean_args = _AT_MENTION_RE.sub('', args).strip()
            clean_args = _CQ_CODE_RE.sub('', clean_args).strip()

            parts = clean_args.split() if clean_args else []

            # 分类参数：数字→分数，VALID_PATHS→命途，VALID_CLASSES→职业，其他→姓名
            other_words = []

            for p in parts:
                if p.isdigit():
                    scores.append(int(p))
                elif p in VALID_PATHS:
                    explicit_faith = p
                elif p in VALID_CLASSES:
                    explicit_class = p
                else:
                    other_words.append(p)

            # 非数字/信仰/职业的词，第一个作为玩家名
            if other_words:
                explicit_name = other_words[0]

        # 合并：显式 > 自动提取
        player_name = explicit_name or auto_name
        faith_name = explicit_faith or auto_faith
        class_name = explicit_class or auto_class

        if at_user_id and target_member:
            member_id = str(target_member.get("user_id", at_user_id))
            target_card = target_member.get("card") or target_member.get("nickname") or member_id
        else:
            member_id = None
            target_card = None

        # 校验必填项
        errors = []
        if not player_name:
            errors.append("玩家名（未从名片识别到，请在参数中指定）")
        if not faith_name:
            errors.append(f"命途（可选: {'/'.join(VALID_PATHS)}）")
        if not class_name:
            errors.append(f"职业（可选: {'/'.join(VALID_CLASSES)}）")

        if errors:
            auto_info = ""
            if at_user_id:
                auto_info = f"\n从名片自动提取: 姓名={auto_name or '?'}, 信仰={auto_faith or '?'}, 职业={auto_class or '?'}"
            yield event.plain_result(
                f"缺少必要参数: {', '.join(errors)}\n"
                f"用法：录入玩家 @用户 [姓名] [信仰] [职业] [登神之路分] [觐见分]\n"
                f"  或: 录入玩家 <姓名> <信仰> <职业> [登神之路分] [觐见分]{auto_info}"
            )
            return

        max_name_len = self.config.get("player_name_max_length", 20)
        if len(player_name) > max_name_len:
            yield event.plain_result(f"玩家名过长，最长 {max_name_len} 个字符。")
            return

        if faith_name not in VALID_PATHS:
            yield event.plain_result(f"无效的命途: {faith_name}。可选: {'/'.join(VALID_PATHS)}")
            return

        if class_name not in VALID_CLASSES:
            yield event.plain_result(f"无效的职业: {class_name}。可选: {'/'.join(VALID_CLASSES)}")
            return

        # 分数处理
        if len(scores) >= 2:
            ladder_score, pilgrimage_score = scores[0], scores[1]
        elif len(scores) == 1:
            ladder_score = scores[0]
            pilgrimage_score = self.config.get("init_pilgrimage_score", 100)
        else:
            ladder_score = self.config.get("init_ladder_score", 1000)
            pilgrimage_score = self.config.get("init_pilgrimage_score", 100)

        # 检查玩家是否已存在
        existing = await self.db_manager.get_player_by_name(group_id, player_name)
        if existing:
            yield event.plain_result(f"玩家 {player_name} 已存在。")
            return

        # 如果没有通过 @ 找到目标，尝试通过玩家名匹配
        if not target_member:
            target_member = await self._find_member_by_name(event, player_name)

        target_info = ""
        if target_member:
            member_id = str(target_member.get("user_id", at_user_id or ""))
            target_card = target_member.get("card") or target_member.get("nickname") or member_id
            target_info = f"（对应群名片：{target_card}，QQ: {member_id}）"

        # 直接注册玩家
        # @ 路径：绑定被录入者的 QQ；无 @ 路径：不自动绑定（避免诸神录入者自己的 QQ 被占用）
        qq_to_bind = at_user_id if at_user_id else None
        success, message = await self.ladder_service.register_player(
            group_id, player_name, faith_name, class_name,
            ladder_score, pilgrimage_score, user_id, qq_id=qq_to_bind
        )

        # 注册成功时，如果使用了@，则@被录入玩家并提醒
        if success and at_user_id:
            from astrbot.core.message.components import At, Plain
            yield event.chain_result([
                At(qq=int(at_user_id)),
                Plain(text=" 请及时进行谕行（或说出祷词），否则将取消录入\n已自动绑定你的 QQ，后续可使用需鉴权的指令。")
            ])
        else:
            yield event.plain_result(message + target_info)

    # === 绑定 QQ ===

    @filter.command("绑定QQ", alias={"bindqq", "绑定qq"})
    async def cmd_bind_qq(self, event: AstrMessageEvent):
        """为指定玩家绑定 QQ（诸神权限）。
        格式：绑定QQ @用户 或 绑定QQ <玩家名>
        一个 QQ 在同一群只能绑定一个玩家。"""
        group_id = self._get_group_id(event)
        if not await self._check_perm(event):
            yield event.plain_result("只有诸神才能为玩家绑定 QQ。")
            return

        at_user_id = await self._get_at_user_id(event)
        args = self._get_args(event, "绑定QQ")

        # 解析目标 QQ 和目标玩家
        target_qq = None
        player = None

        # 提取参数中的第一个词作为候选玩家名（用于 @ 解析失败时回退）
        cleaned = _CQ_CODE_RE.sub('', args).strip() if args else ""
        parts = cleaned.split()
        player_name_arg = parts[0] if parts else None

        if at_user_id:
            target_qq = str(at_user_id)
            player, err = await self._resolve_player_by_at(group_id, at_user_id, event)
            if err:
                yield event.plain_result(err)
                return
        if not player and player_name_arg:
            # @ 解析不出 → 尝试参数里的玩家名
            player = await self.db_manager.get_player_by_name(group_id, player_name_arg)
            if not player:
                yield event.plain_result(f"玩家 {player_name_arg} 不存在。")
                return
            if not target_qq:
                # 没 @ 走的是玩家名路径 → 反查其 QQ
                member = await self._find_member_by_name(event, player.player_name)
                if not member:
                    yield event.plain_result(
                        f"无法在群成员中找到 {player.player_name}，请改用「绑定QQ @用户」格式。"
                    )
                    return
                target_qq = str(member.get("user_id"))

        if not player or not target_qq:
            yield event.plain_result(
                "用法：绑定QQ @用户\n"
                "   或：绑定QQ <玩家名>"
            )
            return

        # 检查 QQ 唯一约束
        existing = await self.db_manager.get_player_by_qq(group_id, target_qq)
        if existing and existing.player_id != player.player_id:
            yield event.plain_result(
                f"QQ {target_qq} 已绑定到玩家 {existing.player_name}，"
                "一个 QQ 在同一群只能绑定一个玩家。"
            )
            return

        if player.qq_id == target_qq:
            yield event.plain_result(f"玩家 {player.player_name} 已绑定 QQ {target_qq}，无需重复绑定。")
            return

        ok = await self.db_manager.set_player_qq(group_id, player.player_id, target_qq)
        if not ok:
            yield event.plain_result("绑定失败：QQ 已被其他玩家绑定。")
            return

        yield event.plain_result(
            f"已绑定：玩家 {player.player_name} ↔ QQ {target_qq}\n"
            "后续该玩家可使用需鉴权的指令（赠送/接受/拒绝道具等）。"
        )

    # === 换绑 QQ ===

    @filter.command("换绑QQ", alias={"rebindqq", "换绑qq"})
    async def cmd_rebind_qq(self, event: AstrMessageEvent):
        """为玩家换绑 QQ（诸神权限）。
        格式：
          换绑QQ @新QQ所属用户 <玩家名>    — 把玩家绑到 @ 用户的 QQ
          换绑QQ <玩家名> <新QQ号>          — 直接指定新 QQ 号
        若新 QQ 已绑其他玩家，提示先解绑/换绑。"""
        group_id = self._get_group_id(event)
        if not await self._check_perm(event):
            yield event.plain_result("只有诸神才能为玩家换绑 QQ。")
            return

        args = self._get_args(event, "换绑QQ")
        cleaned = _CQ_CODE_RE.sub('', args).strip() if args else ""
        parts = cleaned.split()
        at_user_id = await self._get_at_user_id(event)

        new_qq = None
        player = None

        if at_user_id:
            # 路径 1：@ 指定新 QQ 所属用户，参数里给玩家名
            new_qq = str(at_user_id)
            if not parts:
                yield event.plain_result(
                    "用法：换绑QQ @新QQ用户 <玩家名>\n"
                    "请指定要换绑的玩家名（@ 的 QQ 将成为新绑定）。"
                )
                return
            player = await self.db_manager.get_player_by_name(group_id, parts[0])
            if not player:
                yield event.plain_result(f"玩家 {parts[0]} 不存在。")
                return
        else:
            # 路径 2：<玩家名> <新QQ号>
            if len(parts) < 2:
                yield event.plain_result(
                    "用法：换绑QQ @新QQ用户 <玩家名>\n"
                    "   或：换绑QQ <玩家名> <新QQ号>"
                )
                return
            player = await self.db_manager.get_player_by_name(group_id, parts[0])
            if not player:
                yield event.plain_result(f"玩家 {parts[0]} 不存在。")
                return
            new_qq = parts[1]

        # 执行换绑
        ok, msg, old_qq = await self.db_manager.rebind_player_qq(
            group_id, player.player_id, new_qq
        )
        if not ok:
            yield event.plain_result(msg)
            return

        if old_qq:
            yield event.plain_result(
                f"已换绑：玩家 {player.player_name} 的 QQ {old_qq} → {new_qq}"
            )
        else:
            yield event.plain_result(
                f"已绑定：玩家 {player.player_name} 首次绑定 QQ {new_qq}"
            )

    # === 设置职业（仅职业） ===

    @filter.command("设置职业", alias={"setclass", "改职业"})
    async def cmd_set_class(self, event: AstrMessageEvent):
        """修改玩家职业。格式: 设置职业 <玩家名> <职业>"""
        group_id = self._get_group_id(event)

        if not await self._check_perm(event):
            yield event.plain_result("凡人也胆敢染指神明的权柄？")
            return

        args = self._get_args(event, "设置职业")
        if not args:
            args = self._get_args(event, "setclass") or self._get_args(event, "改职业")

        parts = args.split()
        if len(parts) != 2:
            yield event.plain_result(
                f"用法：设置职业 <玩家名> <职业>\n"
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

        if not await self._check_perm(event):
            yield event.plain_result("凡人也胆敢染指神明的权柄？")
            return

        # Cooldown
        cooldown_seconds = self.config.get("ladder_cooldown_seconds", 600)
        user_id = str(event.get_sender_id())
        cd_key = f"{user_id}:oath"
        if not self.cooldown_manager.check_cooldown(cd_key, cooldown_seconds):
            remaining = self.cooldown_manager.get_remaining(cd_key, cooldown_seconds)
            yield event.plain_result(f"冷却中，请 {remaining:.0f} 秒后再试。")
            return
        self.cooldown_manager.set_cooldown(cd_key)

        args = self._get_args(event, "立誓")
        if not args:
            args = self._get_args(event, "takeoath") or self._get_args(event, "立约")

        parts = args.split()
        if len(parts) != 2:
            yield event.plain_result(
                f"用法：立誓 <玩家名> <命途>\n"
                f"可选命途: {'/'.join(VALID_PATHS)}"
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

        if not await self._check_perm(event):
            yield event.plain_result("凡人也胆敢染指神明的权柄？")
            return

        # Cooldown
        cooldown_seconds = self.config.get("ladder_cooldown_seconds", 600)
        cd_key = f"{user_id}:oath"
        if not self.cooldown_manager.check_cooldown(cd_key, cooldown_seconds):
            remaining = self.cooldown_manager.get_remaining(cd_key, cooldown_seconds)
            yield event.plain_result(f"冷却中，请 {remaining:.0f} 秒后再试。")
            return
        self.cooldown_manager.set_cooldown(cd_key)

        args = self._get_args(event, "弃誓")
        if not args:
            args = self._get_args(event, "abandoath")

        parts = args.split()
        if not parts or len(parts) > 2:
            yield event.plain_result(
                f"用法：弃誓 <玩家名> [新信仰]\n"
                f"示例：弃誓 张三\n"
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
        """管理员/诸神操作。格式: 天梯榜管理 <操作> [参数]"""
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
                f"删除/ delete <玩家名> — 删除单个玩家 (诸神/管理员)\n"
                f"改名/ rename <旧名> <新名> — 改名 (诸神/管理员)\n"
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
            if not await self._check_perm(event):
                yield event.plain_result("仅供诸神使用")
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
                yield event.plain_result("仅供诸神使用")
                return
            if len(parts) < 3:
                yield event.plain_result("用法：天梯榜管理 改名 <旧名> <新名>")
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
            self.ladder_service.invalidate_leaderboard_cache(group_id)
            yield event.plain_result(f"已重置本群 {count} 名玩家的积分（天梯: {init_ladder}, 觐见: {init_pilgrimage}）。")

        elif action == "clear":
            count = await self.db_manager.delete_all_players(group_id)
            self.ladder_service.invalidate_leaderboard_cache(group_id)
            yield event.plain_result(f"已清空本群所有数据，共删除 {count} 名玩家。")

        else:
            yield event.plain_result(f"未知操作: {action}\n发送「天梯榜管理」查看所有可用操作。")

    # === 白名单 ===

    @filter.command("白名单", alias={"whitelist", "wl"})
    async def cmd_whitelist(self, event: AstrMessageEvent):
        """白名单管理。格式: 白名单 <add/remove/list> [类型] [ID]"""
        if not self._is_plugin_admin(event):
            yield event.plain_result( "权限不足：仅管理员可管理诸神列表。")
            return

        user_id = str(event.get_sender_id())
        args = self._get_args(event, "白名单")
        if not args:
            args = self._get_args(event, "whitelist") or self._get_args(event, "wl")

        parts = args.split()
        if not parts:
            yield event.plain_result(
                f"用法：白名单 <add/remove/list> [类型] [ID]\n"
                f"类型: user (用户) 或 group (群)"
            )
            return

        action = parts[0]
        if action == "list":
            text = await self.permission_service.get_whitelist_text()
            yield event.plain_result( text)
        elif action == "add" and len(parts) >= 3:
            _, message = await self.permission_service.add_to_whitelist(parts[1], parts[2], user_id)
            # 白名单变更后失效权限缓存（让新权限立即生效）
            self.permission_service.invalidate_cache()
            yield event.plain_result( message)
        elif action == "remove" and len(parts) >= 3:
            _, message = await self.permission_service.remove_from_whitelist(parts[1], parts[2])
            # 白名单变更后失效权限缓存（让权限移除立即生效）
            self.permission_service.invalidate_cache()
            yield event.plain_result( message)
        else:
            yield event.plain_result(f"用法：白名单 <add/remove/list> [类型] [ID]")

    # === 帮助 ===

    @filter.command("天梯榜帮助", alias={"ladderhelp"})
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

        has_perm = await self._check_perm(event)

        args = self._get_args(event, "查询储物空间")
        args = args.strip() if args else ""

        if has_perm:
            # 诸神/管理员：必须指定目标
            if not args:
                yield event.plain_result("用法：查询储物空间 <玩家名> [玩家名2 ...]")
                return
            names = args.split()
        else:
            # 非诸神：只能查自己，无视后面的参数（优先 QQ 绑定，回退名片识别）
            self_player = await self._resolve_self_player_lenient(event)
            if not self_player:
                yield event.plain_result(
                    "无法识别你的身份，请先让诸神为你「绑定QQ」或确认群名片格式正确。"
                )
                return
            names = [self_player.player_name]

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

        if not await self._check_perm(event):
            yield event.plain_result("权限不足。")
            return

        args = self._get_args(event, "赐予道具")
        if not args:
            yield event.plain_result("用法：赐予道具 <玩家名> <道具*数量> ...\n示例：赐予道具 张三 铁剑*2 生命药水*3")
            return

        parts = args.split(None, 1)  # 分割为玩家名 + 剩余
        if len(parts) < 2:
            yield event.plain_result("用法：赐予道具 <玩家名> <道具*数量> ...")
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

        if not await self._check_perm(event):
            yield event.plain_result("权限不足。")
            return

        args = self._get_args(event, "收回道具")
        if not args:
            yield event.plain_result("用法：收回道具 <玩家名> <道具*数量> ...\n示例：收回道具 张三 铁剑*2 生命药水")
            return

        parts = args.split(None, 1)
        if len(parts) < 2:
            yield event.plain_result("用法：收回道具 <玩家名> <道具*数量> ...")
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

        if not await self._check_perm(event):
            yield event.plain_result("权限不足。")
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

    # === 白名单自动同步 ===

    @filter.command("同步白名单")
    async def cmd_sync_whitelist(self, event: AstrMessageEvent):
        """同步指定群的当前成员到白名单。格式: 同步白名单"""
        if not self._is_plugin_admin(event):
            yield event.plain_result("权限不足：仅管理员可同步诸神列表。")
            return

        target_group = self.config.get("auto_whitelist_group", "")
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
            success, _ = await self.permission_service.add_to_whitelist("user", uid, "sync")
            if success:
                added += 1

        # 同步完成后失效权限缓存
        if added > 0:
            self.permission_service.invalidate_cache()

        yield event.plain_result(f"诸神列表同步完成: 新增 {added} 人（群 {target_group} 共 {len(members)} 名成员）")

    async def _handle_auto_whitelist(self, user_id: str, action: str):
        """处理白名单自动同步（加入/离开指定群）。"""
        target_group = self.config.get("auto_whitelist_group", "")
        if not target_group:
            return

        if action == "join":
            success, msg = await self.permission_service.add_to_whitelist("user", user_id, "auto")
            if success:
                logger.info(f"[AutoWhitelist] 自动添加白名单: {user_id}")
                self.permission_service.invalidate_cache(user_id)
        elif action == "leave":
            success, msg = await self.permission_service.remove_from_whitelist("user", user_id)
            if success:
                logger.info(f"[AutoWhitelist] 自动移除白名单: {user_id}")
                self.permission_service.invalidate_cache(user_id)

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

    # === 状态 ===

    @filter.command("添加状态")
    async def cmd_add_status(self, event: AstrMessageEvent):
        """添加状态。格式: 添加状态 <玩家名> <状态名> <天数>"""
        group_id = self._get_group_id(event)
        user_id = str(event.get_sender_id())

        if not await self._check_perm(event):
            yield event.plain_result("权限不足。")
            return

        args = self._get_args(event, "添加状态")
        if not args:
            yield event.plain_result("用法：添加状态 <玩家名> <状态名> <天数>\n示例：添加状态 繁荣 虚弱 3")
            return

        parts = args.split()
        if len(parts) < 3:
            yield event.plain_result("用法：添加状态 <玩家名> <状态名> <天数>")
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

        if not await self._check_perm(event):
            yield event.plain_result("权限不足。")
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

    @filter.command("清除状态")
    async def cmd_clear_status(self, event: AstrMessageEvent):
        """清除所有状态。格式: 清除状态 <玩家名>"""
        group_id = self._get_group_id(event)
        user_id = str(event.get_sender_id())

        if not await self._check_perm(event):
            yield event.plain_result("权限不足。")
            return

        args = self._get_args(event, "清除状态")
        if not args or not args.strip():
            yield event.plain_result("用法：清除状态 <玩家名>")
            return

        player_name = args.strip()
        success, message = await self.ladder_service.clear_statuses(group_id, player_name)
        yield event.plain_result(message)

    # === 赠送道具 ===

    @filter.command("赠送道具")
    async def cmd_gift_item(self, event: AstrMessageEvent):
        """赠送道具。格式: 赠送道具 <接收方名> <道具*数量>
        发送方由发送者 QQ 绑定鉴权（防名片冒充），接收方仍按玩家名查找。"""
        group_id = self._get_group_id(event)
        await self._maybe_trigger_qq_migration(event)

        # 发送方 = 自己（QQ 绑定鉴权）
        sender_player = await self._resolve_self_player(event)
        if not sender_player:
            name = await self._resolve_player_name(event)
            if name:
                yield event.plain_result(
                    f"玩家 {name} 尚未绑定 QQ，无法赠送道具。\n"
                    "请让诸神使用「绑定QQ @你」完成绑定。"
                )
            else:
                yield event.plain_result(
                    "你尚未绑定 QQ，无法赠送道具。\n"
                    "请让诸神使用「绑定QQ @你」完成绑定。"
                )
            return
        sender_name = sender_player.player_name

        args = self._get_args(event, "赠送道具")
        if not args:
            yield event.plain_result("用法：赠送道具 <接收方名> <道具*数量>\n示例：赠送道具 Bob 铁剑*3")
            return

        parts = args.split(None, 1)
        if len(parts) < 2:
            yield event.plain_result("用法：赠送道具 <接收方名> <道具*数量>")
            return

        receiver_name, item_args = parts[0], parts[1].strip()

        # 解析道具（只支持一种）
        items = self._parse_item_args(item_args)
        if not items:
            yield event.plain_result("未指定有效道具。格式: 道具名*数量")
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

        # 检查接收方是否已有待处理的赠送
        gift_key = (group_id, str(receiver_player.player_id))
        existing_gift = self._pending_gifts_receive.get(gift_key)
        if not existing_gift:
            existing_gift = await self.db_manager.get_pending_gift(group_id, str(receiver_player.player_id))
        if existing_gift:
            yield event.plain_result(f"{receiver_name} 有未处理的赠送，请先接受或拒绝后再发起新的赠送。")
            return

        # 直接扣除发送方道具
        success, msg, base_name, grade = await self.ladder_service.deduct_item(
            group_id, sender_player.player_id, sender_name, item_raw, quantity
        )
        if not success:
            yield event.plain_result(msg)
            return

        # 存入接收方待确认（内存 + DB 双写）
        import json
        gift_key = (group_id, str(receiver_player.player_id))
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
        self._pending_gifts_receive[gift_key] = gift_data
        await self.db_manager.save_pending_gift(
            group_id, str(receiver_player.player_id),
            str(sender_player.player_id), sender_name, receiver_name,
            json.dumps({"item_name": base_name, "grade": grade, "quantity": quantity})
        )

        from astrbot_plugin_faith_ladder.message_formatter import format_gift_request
        notification = format_gift_request(
            sender_name, receiver_name, base_name, grade, quantity
        )
        yield event.plain_result(
            f"已从 {sender_name} 扣除，等待 {receiver_name} 接受。\n\n{notification}"
        )

    @filter.command("接受道具")
    async def cmd_accept_gift(self, event: AstrMessageEvent):
        """接收方接受赠送，无需参数。诸神可带参数指定接收玩家（跳过名片检测）。"""
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
                cleaned = _CQ_CODE_RE.sub('', args).strip()
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

        # 检查今日接受道具次数（可配置上限，0 为不限制）
        daily_limit = self.config.get("gift_daily_accept_limit", 1)
        if daily_limit > 0:
            accept_count = await self.db_manager.count_gift_accepts_today(group_id, receiver_id)
            if accept_count >= daily_limit:
                yield event.plain_result(f"今日接受道具次数已达上限（{daily_limit} 次/天）。")
                return

        gift = await self._get_valid_pending_gift(group_id, receiver_id)
        if not gift:
            yield event.plain_result("没有待接受的赠送（或赠送已超时退回）。")
            return

        # 记录今日已接受道具（用于计数）
        await self.db_manager.record_gift_accept(group_id, receiver_id)

        gift_key = (group_id, receiver_id)
        from astrbot_plugin_faith_ladder.ladder_service import format_item_display
        success, msg = await self.ladder_service.receive_item(
            gift["group_id"], gift["receiver_id"], gift["receiver_name"],
            gift["item_name"], gift["quantity"], grade=gift.get("grade")
        )
        self._pending_gifts_receive.pop(gift_key, None)
        await self.db_manager.delete_pending_gift(group_id, receiver_id)

        if success:
            display = format_item_display(gift["item_name"], gift.get("grade"), gift["quantity"])
            yield event.plain_result(
                f"已接受 {gift['sender_name']} 赠送的 {display}"
            )
        else:
            yield event.plain_result(f"接受失败：{msg}")

    @filter.command("拒绝道具")
    async def cmd_reject_gift(self, event: AstrMessageEvent):
        """接收方拒绝赠送，无需参数。诸神可带参数指定接收玩家（跳过 QQ 绑定检测）。"""
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
                cleaned = _CQ_CODE_RE.sub('', args).strip()
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

        gift_key = (group_id, receiver_id)
        from astrbot_plugin_faith_ladder.ladder_service import format_item_display
        # 退回赠送方道具
        await self.ladder_service.receive_item(
            gift["group_id"], gift["sender_id"], gift["sender_name"],
            gift["item_name"], gift["quantity"], grade=gift.get("grade")
        )
        self._pending_gifts_receive.pop(gift_key, None)
        await self.db_manager.delete_pending_gift(group_id, receiver_id)

        display = format_item_display(gift["item_name"], gift.get("grade"), gift["quantity"])
        yield event.plain_result(
            f"已拒绝 {gift['sender_name']} 的赠送，{display} 已退回"
        )

    # ── 祷词触发 ──

    def _build_prayer_cache(self):
        """构建祷词缓存：{归一化祷词: 命途名}。启动时和配置变更时调用。"""
        self._prayer_cache = {}
        # 祷词配置按命途（path）存储，不是按信仰（faith）
        for path in VALID_PATHS:
            key = f"prayer_text_{path}"
            prayers = self.config.get(key, [])
            for prayer in prayers:
                normalized = self._normalize_prayer_text(prayer)
                if normalized:
                    self._prayer_cache[normalized] = path

        # 缓存命令前缀
        cmd_keys = [
            "cmd_ladder", "cmd_pilgrimage", "cmd_query", "cmd_add_score",
            "cmd_set_class", "cmd_register_player", "cmd_admin", "cmd_whitelist",
            "cmd_help", "cmd_batch_add_score", "cmd_abandon_oath", "cmd_take_oath"
        ]
        self._command_prefixes = {
            self.config.get(key, "") for key in cmd_keys if self.config.get(key)
        }

    def _normalize_prayer_text(self, text: str) -> str:
        """去除所有标点和空格，仅保留中文字符和字母数字。"""
        return _PRAYER_NORMALIZE_RE.sub('', text).strip()

    def _quick_chinese_count(self, text: str) -> int:
        """快速统计汉字数量（不做完整归一化）。"""
        return sum(1 for c in text if '一' <= c <= '鿿')

    def _is_valid_prayer_length(self, text: str) -> bool:
        """检查是否为恰好 8 个汉字（祷词固定长度，快速过滤非祷词消息）。"""
        chinese_chars = _PRAYER_CHINESE_RE.findall(text)
        return len(chinese_chars) == 8

    def _is_command_message(self, text: str) -> bool:
        """检查消息是否以已注册的命令前缀开头。"""
        text_stripped = text.strip()
        return any(text_stripped.startswith(prefix) for prefix in self._command_prefixes if prefix)

    @filter.regex(r".*")
    async def on_prayer_message(self, event: AstrMessageEvent, matched=None):
        """监听所有消息，检测祷词触发。"""
        logger.debug(f"[PrayerTrigger] Message received: {event.message_str}")

        # 1. 快速过滤：必须是群消息（非私聊）
        if not hasattr(event.message_obj, 'group_id') or not event.message_obj.group_id:
            logger.debug("[PrayerTrigger] Not a group message")
            return

        group_id = self._get_group_id(event)
        logger.debug(f"[PrayerTrigger] Group: {group_id}")

        # 2. 快速过滤：群是否在配置列表中
        trigger_groups = self.config.get("prayer_trigger_groups", [])
        logger.debug(f"[PrayerTrigger] Trigger groups: {trigger_groups}")
        if group_id not in trigger_groups:
            logger.debug(f"[PrayerTrigger] Group {group_id} not in trigger list")
            return

        # 3. 获取消息纯文本
        text = event.message_str
        if not text:
            return

        # 4. 快速过滤：不是命令才处理（避免与命令冲突）
        if self._is_command_message(text):
            return

        # 5. 快速长度预过滤（含标点空格，祷词 8 字 + 最多 12 个标点 = 20）
        text_len = len(text)
        if text_len < 8 or text_len > 20:
            logger.debug(f"[PrayerTrigger] Length pre-filter: {text_len} chars, skipped")
            return

        # 6. 快速汉字计数（恰好 8 个汉字才继续）
        if self._quick_chinese_count(text) != 8:
            logger.debug("[PrayerTrigger] Chinese count != 8, skipped")
            return

        # 7. 现在才做完整归一化（仅对潜在祷词消息）
        normalized = self._normalize_prayer_text(text)
        if not normalized:
            return

        # 8. 快速匹配：是否匹配任何祷词（缓存查找，O(1)）
        matched_faith = self._prayer_cache.get(normalized)
        logger.debug(f"[PrayerTrigger] Matched faith: {matched_faith}, cache size: {len(self._prayer_cache)}")
        if not matched_faith:
            logger.debug("[PrayerTrigger] No prayer match")
            return

        # 9. 现在才解析玩家身份（昂贵操作，仅对潜在祷词消息执行）
        player = await self._resolve_self_player_lenient(event)
        logger.debug(f"[PrayerTrigger] Player resolved: {player.player_name if player else None}, faith: {player.faith if player else None}")
        if not player or not player.faith:
            logger.debug("[PrayerTrigger] Player not found or no faith")
            return

        # 10. 检查是否匹配玩家自己的命途（发送其他命途的祷词不触发）
        if player.faith != matched_faith:
            logger.debug(f"[PrayerTrigger] Player faith {player.faith} != matched {matched_faith}")
            return

        # 11. 检查今日是否已触发（DB 查询）
        if await self.db_manager.has_prayer_hit_today(group_id, player.player_id):
            return

        # 12. 随机 -2 到 +2（暂时禁用，固定为 0，后续可能启用）
        # import random
        # delta = random.randint(-2, 2)
        # # 如果配置不允许负分，则 clamp 到 [0, 2]
        # if not self.config.get("allow_negative_scores", True):
        #     delta = max(0, delta)
        delta = 0  # 暂时不加分也不扣分

        # 13. 记录今日已触发（DB 写入，唯一约束防并发）
        recorded = await self.db_manager.record_prayer_hit(group_id, player.player_id, delta)
        if not recorded:
            return  # 并发情况，已被其他请求抢先

        # 14. 加分（ladder_delta=0, pilgrimage_delta=delta）
        # 暂时 delta=0，不会实际改变分数，但仍记录触发
        if delta != 0:
            ok, _ = await self.ladder_service.add_score(
                group_id, player.player_id, player.player_name,
                ladder_delta=0, pilgrimage_delta=delta,
                operator_id="prayer_trigger",
                reason="祷词触发"
            )
            if not ok:
                return

        # 15. 回复群消息 + 阻止 AI 也响应祷词
        msg = format_prayer_trigger(player.player_name, player.faith, delta, self.config)
        yield event.plain_result(msg)
        event.stop_event()
