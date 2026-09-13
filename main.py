"""
信仰游戏天梯排行榜 - Faith Game Ladder Plugin for AstrBot
A dual-ladder ranking system with class/faith customization for group chats.
"""

import sys
import re
import json
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
from astrbot_plugin_faith_ladder.models import VALID_CLASSES, VALID_FAITHS, VALID_PATHS, FAITH_TO_PATH, Player
from astrbot_plugin_faith_ladder.item_utils import extract_item_quantity, parse_item_args
from astrbot_plugin_faith_ladder.qq_admin_handle import QQAdminHandler
from astrbot_plugin_faith_ladder.messages import (
    PERMISSION_DENIED, PLAYER_NOT_FOUND,
    INVALID_ITEM_FORMAT,
    BATCH_ALL_SUCCESS, BATCH_PARTIAL_SKIP,
    COOLDOWN_MSG, BATCH_COOLDOWN_MSG, OATH_COOLDOWN_MSG,
)
from astrbot_plugin_faith_ladder.faith_messages import FAITH_MESSAGES, GENERIC_GOD_MESSAGES
from astrbot_plugin_faith_ladder.commands import QueryCommandsMixin, SharedSendMixin

# 预编译正则（祷词触发用）
_PRAYER_NORMALIZE_RE = re.compile(r'[^\w]')
_SPECIFIC_FAITH_TAG_RE = re.compile(r'【([^】]+)】')

# 预编译正则（通用）
_CQ_CODE_RE = re.compile(r'\[CQ:[^\]]+\]')
_AT_MENTION_RE = re.compile(r'@\S+')
_CARD_BRACKET_RE = re.compile(r'^【[^】]*】\s*(.*)')
_CARD_CONTENT_RE = re.compile(r'^【([^】]*)】\s*(.*)')


@register(
    "astrbot_plugin_faith_ladder",
    "custom",
    "双积分排名插件，登神之路+觐见之梯双榜展示，支持弃誓/立誓系统、批量录入、道具储物空间与赠送、QQ群管指令，适用于社群活动积分管理。仅支持群聊使用。",
    "3.6.5"
)
class FaithLadderPlugin(QueryCommandsMixin, SharedSendMixin, Star):
    """信仰游戏天梯排行榜插件。

    指令的装饰器一律留在本类上；实现体按职责放在 commands/ 下的 mixin 中
    （见 commands/__init__.py 的约定说明）。
    """

    def __init__(self, context: Context, config=None):
        """初始化插件：解析数据目录、装配各服务与缓存（不含 IO，实际初始化见 initialize）。"""
        super().__init__(context)
        self.config = config or {}
        self.data_dir = self._get_data_dir()
        self.db_manager = DatabaseManager(self.data_dir)
        self.ladder_service = LadderService(self.db_manager)
        self.cooldown_manager = CooldownManager()

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
        self._qq_admin = QQAdminHandler(
            check_perm_fn=self.permission_service.check_score_permission,
            check_admin_fn=self._is_plugin_admin,
            get_faith_fn=self._get_god_faith,
        )
        self._pending_gifts_receive = {}  # (group_id, receiver_id) -> gift_dict（内存缓存）

        # 祷词触发缓存
        self._prayer_cache = {}  # {normalized_prayer: faith}
        self._command_prefixes = set()
        self._build_prayer_cache()

        # 加载具体职业映射
        self._specific_classes = {}  # specific_class_name -> (faith, basic_class)
        # 先给排序结果一个空默认值：_load_specific_classes 失败时只 log 不抛，
        # 若这里不初始化，后续 _parse_card_info 访问它会是 AttributeError
        self._sorted_specific_classes = []
        self._load_specific_classes()

    def _get_data_dir(self) -> Path:
        """获取插件数据目录，符合 AstrBot 规范：data/plugin_data/<plugin_name>/"""
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path
            data_path = Path(get_astrbot_data_path())
            return data_path / "plugin_data" / "astrbot_plugin_faith_ladder"
        except Exception:
            pass

        # 回退：尝试从 context 获取
        for method_name in ("get_data_path", "get_astrbot_data_path"):
            method = getattr(self.context, method_name, None)
            if method and callable(method):
                try:
                    result = method()
                    if result:
                        return Path(result) / "plugin_data" / "astrbot_plugin_faith_ladder"
                except Exception:
                    continue

        # 最终回退：插件目录下的 data 文件夹
        return _plugin_dir / "data"

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
            # 预排序（按职业名长度降序，确保长名优先匹配）
            self._sorted_specific_classes = sorted(
                self._specific_classes.items(), key=lambda x: len(x[0]), reverse=True
            )
            logger.info(f"[SpecificClasses] 加载 {len(self._specific_classes)} 个具体职业映射")
        except Exception as e:
            logger.warning(f"[SpecificClasses] 加载具体职业映射失败: {e}")

    async def initialize(self):
        """插件加载：建库与迁移、启动调度器、注册群成员变动监听。"""
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
            on_gift_refunded=self._forget_pending_gift_cache,
            backup_db=self.db_manager.backup_to,
        )
        await self._scheduler.start()

        # 注册群成员变动监听（白名单自动同步）
        # 注意：不同 AstrBot 版本的 Context 不一定提供 register_event_handler。
        # 缺失时此前是静默跳过（连日志都没有），表现为「自动白名单看似开着却从不同步」，
        # 因此这里显式告警，方便判断该功能是否真的生效。
        try:
            if hasattr(self.context, 'register_event_handler'):
                self.context.register_event_handler(self.on_group_member_change)
                logger.info("[AutoWhitelist] 群成员变动监听已注册")
            else:
                logger.warning(
                    "[AutoWhitelist] 当前 AstrBot 版本不支持 register_event_handler，"
                    "群成员加入/退出不会自动同步白名单；可用「同步白名单」命令手动同步"
                )
        except Exception as e:
            logger.warning(f"[AutoWhitelist] 事件监听注册失败（可用'同步白名单'命令手动同步）: {e}")

        logger.info("FaithLadder plugin initialized")

    async def terminate(self):
        """插件卸载：停止调度任务并关闭数据库连接。"""
        if self._scheduler:
            await self._scheduler.stop()
        await self.db_manager.close()
        logger.info("FaithLadder plugin terminated")

    # === Helpers ===

    def _get_group_id(self, event: AstrMessageEvent) -> str:
        """取事件所属群号（字符串）。插件仅支持群聊，私聊场景会取不到 group_id。"""
        return str(event.message_obj.group_id)

    def _get_args(self, event: AstrMessageEvent, cmd_name: str) -> str:
        """取命令名之后的参数文本（精确前缀匹配）。

        会先剥掉 CQ 码与 @ 提及文本：aiocqhttp 适配器会把 "@昵称(QQ)" 拼进
        message_str，不剥掉的话「录入积分 @张三 100 50」会把目标解析成
        "@张三(12345)"，各种「玩家不存在」。需要 @ 目标的指令另有
        _get_at_user_id() 从消息段里取真实 QQ，不受这里影响。
        """
        text = event.message_str.strip()
        if not text.startswith(cmd_name):
            return ""
        args = text[len(cmd_name):].strip()
        args = _CQ_CODE_RE.sub('', args)
        return _AT_MENTION_RE.sub('', args).strip()

    def _is_plugin_admin(self, event: AstrMessageEvent) -> bool:
        """是否为插件管理员（config.admin_ids）。与白名单权限是两套：管理员看配置，诸神看白名单。"""
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

    async def _get_god_faith(self, qq_id: str) -> Optional[str]:
        """获取诸神对应的信仰名（如果在白名单中且配置了信仰）。"""
        return await self.permission_service.get_god_faith(qq_id)

    def _get_faith_message(self, faith: str, action: str, **kwargs) -> str:
        """获取信仰专属的随机消息。优先级：信仰专属 → 通用神明 → 默认。"""
        import random
        messages = FAITH_MESSAGES.get(faith, {}).get(action, [])
        if messages:
            msg = random.choice(messages)
            return msg.format(**kwargs)
        # 回退到通用消息
        generic = GENERIC_GOD_MESSAGES.get(action, [])
        if generic:
            return random.choice(generic).format(**kwargs)
        # 最终回退
        return kwargs.get("default", "")

    async def _resolve_self_player(self, event: AstrMessageEvent) -> Optional[Player]:
        """通过发送者的 QQ 号查找绑定的玩家记录（强鉴权，不可伪造）。"""
        group_id = self._get_group_id(event)
        qq_id = str(event.get_sender_id())
        return await self.db_manager.get_player_by_qq(group_id, qq_id)

    async def _resolve_self_player_lenient(self, event: AstrMessageEvent) -> Optional[Player]:
        """优先 QQ 绑定查找；失败则回退名片识别（兼容未绑定的老玩家）。
        仅用于只读命令（查询/查储物空间）。
        不自动绑定 QQ，避免高频触发。"""
        player = await self._resolve_self_player(event)
        if player:
            return player
        name = await self._resolve_player_name(event)
        if not name:
            return None
        group_id = self._get_group_id(event)
        return await self.db_manager.get_player_by_name(group_id, name)

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

    def _forget_pending_gift_cache(self, group_id: str, receiver_id: str) -> None:
        """清掉内存里的待处理赠送缓存。DB 才是权威来源，缓存只用于减少查询。"""
        self._pending_gifts_receive.pop((group_id, receiver_id), None)

    async def _get_valid_pending_gift(self, group_id: str, receiver_id: str,
                                       max_age_seconds: int = 240) -> Optional[dict]:
        """获取有效的待处理赠送（未超时）。超时则自动退回发送方并返回 None。"""
        gift_key = (group_id, receiver_id)

        # 从 DB 获取（含 created_at）
        db_gift = await self.db_manager.get_pending_gift(group_id, receiver_id)
        if not db_gift:
            self._forget_pending_gift_cache(group_id, receiver_id)
            return None

        # 检查超时
        from datetime import datetime, timezone
        created_at = datetime.strptime(db_gift["created_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - created_at).total_seconds() > max_age_seconds:
            # 超时：先认领（删除记录）再退款，避免与调度器清理并发时重复退款
            self._forget_pending_gift_cache(group_id, receiver_id)
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
        self._pending_gifts_receive[gift_key] = gift
        return gift

    # === 排行榜 ===

    @filter.command("天梯榜", alias={"ladder", "ranking", "排行榜"})
    async def cmd_ladder(self, event: AstrMessageEvent):
        """显示天梯排行榜（需要诸神权限）"""
        user_id = str(event.get_sender_id())

        # Permission check
        if not await self._check_perm(event):
            yield event.plain_result(PERMISSION_DENIED["god_only"])
            return

        # Cooldown check
        cooldown_seconds = self.config.get("ladder_cooldown_seconds", 600)
        cd_key = f"{user_id}:ladder"
        if not self.cooldown_manager.check_cooldown(cd_key, cooldown_seconds):
            remaining = self.cooldown_manager.get_remaining(cd_key, cooldown_seconds)
            yield event.plain_result(COOLDOWN_MSG.format(seconds=f"{remaining:.0f}"))
            return
        self.cooldown_manager.set_cooldown(cd_key)

        group_id = self._get_group_id(event)
        limit = self.config.get("ladder_display_limit", 10)
        text = await self.ladder_service.get_leaderboard_text(group_id, limit)

        # 默认使用合并转发，失败时回退为纯文本
        if await self._send_forward_text(event, group_id, "天梯榜", text):
            return
        yield event.plain_result(text)
        event.stop_event()

    # === 觐见榜 ===

    @filter.command("觐见榜", alias={"pilgrimage", "觐见"})
    async def cmd_pilgrimage(self, event: AstrMessageEvent):
        """显示觐见之梯排行榜（需要诸神权限）"""
        user_id = str(event.get_sender_id())

        # Permission check
        if not await self._check_perm(event):
            yield event.plain_result(PERMISSION_DENIED["god_only"])
            return

        # Cooldown check
        cooldown_seconds = self.config.get("ladder_cooldown_seconds", 600)
        cd_key = f"{user_id}:pilgrimage"
        if not self.cooldown_manager.check_cooldown(cd_key, cooldown_seconds):
            remaining = self.cooldown_manager.get_remaining(cd_key, cooldown_seconds)
            yield event.plain_result(COOLDOWN_MSG.format(seconds=f"{remaining:.0f}"))
            return
        self.cooldown_manager.set_cooldown(cd_key)

        group_id = self._get_group_id(event)
        limit = self.config.get("ladder_display_limit", 10)
        text = await self.ladder_service.get_pilgrimage_leaderboard_text(group_id, limit)

        # 默认使用合并转发，失败时回退为纯文本
        if await self._send_forward_text(event, group_id, "觐见榜", text):
            return
        yield event.plain_result(text)
        event.stop_event()

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
        """自动识别发送者自己的群名片中的玩家名，并提取保存具体信仰。"""
        try:
            sender_id = str(event.get_sender_id())
            group_id = self._get_group_id(event)
            info = await event.bot.get_group_member_info(
                group_id=int(group_id), user_id=int(sender_id)
            )
            card = info.get("card", "") or info.get("nickname", "")
            if card:
                # 提取并保存具体信仰
                specific_faith = self._extract_specific_faith(card)
                if specific_faith:
                    # 通过名片匹配到玩家名后，找到对应玩家记录并保存信仰
                    player_name = await self._resolve_name_from_card(card, group_id)
                    if player_name:
                        player = await self.db_manager.get_player_by_name(group_id, player_name)
                        if player and player.specific_faith != specific_faith:
                            await self.db_manager.set_player_specific_faith(group_id, player.player_id, specific_faith)
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

    def _extract_specific_faith(self, card: str) -> Optional[str]:
        """从群名片的【】标签中提取具体信仰名。
        返回具体信仰名（如"欺诈"），如果不是有效信仰则返回 None。
        """
        match = _SPECIFIC_FAITH_TAG_RE.search(card)
        if match:
            tag = match.group(1).strip()
            if tag in FAITH_TO_PATH:  # 检查是否为有效信仰
                return tag
        return None

    async def _get_at_user_id(self, event: AstrMessageEvent) -> Optional[str]:
        """获取消息中第一个 @ 的用户 ID（排除机器人自身与 @全体成员）。"""
        try:
            from astrbot.core.message.components import At
            for seg in event.get_messages():
                if not isinstance(seg, At):
                    continue
                qq = str(seg.qq)
                # "all" 是 @全体成员 的标识，不是真实 QQ；此前会被当成用户 ID 写进绑定
                if qq == "all" or qq == str(event.get_self_id()):
                    continue
                return qq
        except Exception:
            pass
        return None

    def _parse_card_info(self, card: str) -> dict:
        """从群名片中提取具体信仰、命途、职业、玩家名。

        返回 {"specific_faith": 具体信仰, "faith": 命途, "class_": 职业, "player_name": 玩家名}
        specific_faith 是具体信仰（如"繁荣"，16 个之一），faith 是由它推出的命途
        （如"生命"，6 个之一）；两者都可为 None。

        具体信仰有三个来源，任一命中即可：
        1. 【标签】本身是具体信仰（如【繁荣】）
        2. 具体职业名（specific_classes.json 里每个具体职业都对应一个信仰）
        3. 名片中直接出现的具体信仰词
        规则 3 之前只把这类词从玩家名里剔除、没有利用，等于白丢信息。
        """
        from astrbot_plugin_faith_ladder.models import FAITH_TO_PATH, VALID_FAITHS as SPECIFIC_FAITHS
        result = {"specific_faith": None, "faith": None, "class_": None, "player_name": None}
        card = card.strip()

        # 1. 提取标签
        tag = None
        match = _CARD_CONTENT_RE.match(card)
        if match:
            tag = match.group(1).strip()
            remaining = match.group(2).strip()
        else:
            remaining = card.strip()

        # 标签是具体信仰（如"繁荣"），同时记录信仰与命途
        if tag and tag in SPECIFIC_FAITHS:
            result["specific_faith"] = tag
            result["faith"] = FAITH_TO_PATH.get(tag)

        # 2. 提取非数字词
        words = [w for w in remaining.split() if not w.isdigit()]

        # 3. 找职业（支持职业名后紧跟数字/字母的情况）
        # 按职业名长度降序排序，确保长的优先匹配
        sorted_classes = self._sorted_specific_classes

        class_word = None
        name_parts = []  # 存储玩家名的部分

        for word in words:
            # 检查是否是具体职业（完全匹配或以具体职业开头）
            found_specific = False
            for specific_name, (specific_faith, specific_path, specific_class) in sorted_classes:
                if word == specific_name:
                    # 完全匹配
                    result["class_"] = specific_class
                    if result["specific_faith"] is None:
                        result["specific_faith"] = specific_faith
                    if result["faith"] is None:
                        result["faith"] = specific_path
                    class_word = word
                    found_specific = True
                    break
                elif word.startswith(specific_name) and len(word) > len(specific_name):
                    # 以具体职业开头（如"魔术师1218"）
                    result["class_"] = specific_class
                    if result["specific_faith"] is None:
                        result["specific_faith"] = specific_faith
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

            # 具体信仰词：记录信仰（并推出命途），不计入玩家名
            if word in SPECIFIC_FAITHS:
                if result["specific_faith"] is None:
                    result["specific_faith"] = word
                if result["faith"] is None:
                    result["faith"] = FAITH_TO_PATH.get(word)
                continue

            # 命途词：只推出命途，不计入玩家名
            if word in VALID_PATHS:
                if result["faith"] is None:
                    result["faith"] = word
                continue

            # 其他非关键词加入玩家名
            name_parts.append(word)

        if name_parts:
            result["player_name"] = "".join(name_parts)

        return result

    @filter.command("查询", alias={"query", "查看"})
    async def cmd_query(self, event: AstrMessageEvent):
        """查询玩家信息。格式: 查询（自动识别自己）或 查询 <玩家名>（诸神指定）或 查询 @用户（诸神专用）或 查询 <玩家名1> <玩家名2> ...（诸神批量查询）"""
        async for result in self._query_impl(event):
            yield result

    # === 录入积分 ===

    @filter.command("录入积分", alias={"addscore", "加分"})
    async def cmd_add_score(self, event: AstrMessageEvent):
        """录入积分变化。格式: 录入积分 <玩家名> <天梯分变化> <觐见梯变化>"""
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
            yield event.plain_result(PERMISSION_DENIED["god_only"])
            return

        # Cooldown check（先查冷却，但等参数校验通过后才真正占用 —
        # 否则一条写错的指令会白白烧掉 600 秒冷却，改对了也发不出去）
        cooldown_seconds = self.config.get("ladder_cooldown_seconds", 600)
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
            yield event.plain_result(PERMISSION_DENIED["god_only"])
            return

        args = self._get_args(event, "录入玩家")
        if not args:
            args = self._get_args(event, "register") or self._get_args(event, "添加玩家")

        # 检查是否有 @ 用户
        at_user_id = await self._get_at_user_id(event)
        auto_faith = None
        auto_class = None
        auto_name = None
        auto_specific_faith = None

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
                    auto_specific_faith = parsed["specific_faith"]
            except Exception:
                pass

        # 从参数文本中提取显式值
        # 先剥掉参数里的 @ 提及（aiocqhttp 会把 "@昵称(QQ)" 拼进 message_str），
        # 否则昵称会被当成玩家名。剥掉之后就可以正常解析显式参数了 ——
        # 此前在「@ 且名片解析出姓名」时整段跳过解析，导致显式参数永远无效：
        # 名片里只有姓名时，「录入玩家 @张三 生命 战士」会一直报缺少参数。
        explicit_name = None
        explicit_faith = None
        explicit_class = None
        scores = []

        clean_args = _AT_MENTION_RE.sub('', args).strip()
        clean_args = _CQ_CODE_RE.sub('', clean_args).strip()

        parts = clean_args.split() if clean_args else []

        # 分类参数：数字→分数，VALID_PATHS→命途，VALID_CLASSES→职业，其他→姓名
        other_words = []

        for p in parts:
            try:
                # 用 int() 而非 isdigit()，以支持 +500 / -5 这类带符号分数
                scores.append(int(p))
                continue
            except ValueError:
                pass
            if p in VALID_PATHS:
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

        # 直接注册玩家
        # @ 路径：绑定被录入者的 QQ；无 @ 路径：不自动绑定（避免诸神录入者自己的 QQ 被占用）
        qq_to_bind = at_user_id if at_user_id else None
        success, message = await self.ladder_service.register_player(
            group_id, player_name, faith_name, class_name,
            ladder_score, pilgrimage_score, user_id,
            qq_id=qq_to_bind,
            # 具体信仰来自名片解析（@ 路径才有）；显式参数只能给命途
            specific_faith=auto_specific_faith,
        )

        # 回复统一：注册结果（玩家名/职业/信仰/分数/信仰文案）两条路径都要给出，
        # @ 路径只是额外 @ 被录入者，不再像以前那样把结果整条丢弃。
        # 曾在此拼过「（对应群名片：X，QQ: Y）」与「已自动绑定你的 QQ」，按需求去掉；
        # 后者依赖的"祷词确认后取消录入"机制早已移除，那句后果本就不成立。
        if success and at_user_id:
            from astrbot.core.message.components import At, Plain
            yield event.chain_result([
                At(qq=int(at_user_id)),
                Plain(text=f" {message}")
            ])
        else:
            yield event.plain_result(message)

    # === 检测玩家 ===

    @filter.command("检测玩家", alias={"check", "检测"})
    async def cmd_check_player(self, event: AstrMessageEvent):
        """检测当前玩家的绑定状态（QQ、信仰等），未绑定时自动绑定。"""
        group_id = self._get_group_id(event)

        # 优先 QQ 绑定查找，失败回退名片识别
        player = await self._resolve_self_player_lenient(event)
        if not player:
            # 名片也识别不到，给提示
            yield event.plain_result("无法识别你的身份，请先让诸神为你「绑定QQ」或确认群名片格式正确。")
            return

        # 检查 QQ 绑定状态
        qq_bound = player.qq_id is not None
        sender_qq = str(event.get_sender_id())

        if not qq_bound:
            # 检查该 QQ 是否已被其他玩家绑定
            existing = await self.db_manager.get_player_by_qq(group_id, sender_qq)
            if existing:
                yield event.plain_result(
                    f"检测玩家: {player.player_name}\n"
                    f"命途: {player.faith or '未设定'} | 职业: {player.class_ or '未设定'}\n"
                    f"QQ 状态: 你的 QQ 已被玩家「{existing.player_name}」绑定，无法自动绑定。\n"
                    f"如需换绑请联系诸神使用「换绑QQ」。"
                )
                return

            # 自动绑定（并发下可能被别的请求抢先绑定同一 QQ，返回 False）
            bound = await self.db_manager.set_player_qq(group_id, player.player_id, sender_qq)
            if bound:
                status_line = f"QQ 状态: 已自动绑定 {sender_qq}"
            else:
                status_line = (
                    f"QQ 状态: 自动绑定失败（该 QQ 可能刚被其他玩家绑定）\n"
                    f"如需换绑请联系诸神使用「换绑QQ」。"
                )
        else:
            status_line = f"QQ 状态: 已绑定 {player.qq_id}"

        faith_line = f"命途: {player.faith or '未设定'}"
        if player.specific_faith:
            faith_line += f"（{player.specific_faith}）"

        yield event.plain_result(
            f"检测玩家: {player.player_name}\n"
            f"{faith_line} | 职业: {player.class_ or '未设定'}\n"
            f"{status_line}\n"
            f"登神之路: {player.ladder_score} | 觐见之梯: {player.pilgrimage_score}"
        )

    # === 绑定 QQ ===

    @filter.command("绑定QQ", alias={"bindqq", "绑定qq"})
    async def cmd_bind_qq(self, event: AstrMessageEvent):
        """为指定玩家绑定 QQ（诸神权限）。
        格式：绑定QQ @用户 或 绑定QQ <玩家名>
        一个 QQ 在同一群只能绑定一个玩家。"""
        group_id = self._get_group_id(event)
        if not await self._check_perm(event):
            yield event.plain_result(PERMISSION_DENIED["god_only"])
            return

        at_user_id = await self._get_at_user_id(event)
        args = self._get_args(event, "绑定QQ")
        if not args:
            # 与其它指令一致地支持别名调用（此前用 bindqq 会让参数整个丢掉）
            args = self._get_args(event, "bindqq") or self._get_args(event, "绑定qq")

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
                yield event.plain_result(PLAYER_NOT_FOUND.format(name=player_name_arg))
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
            yield event.plain_result(PERMISSION_DENIED["god_only"])
            return

        args = self._get_args(event, "换绑QQ")
        if not args:
            # 别名调用同样要取到参数（此前用 rebindqq 时参数为空 → 每次都只回用法）
            args = self._get_args(event, "rebindqq") or self._get_args(event, "换绑qq")
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
            if not new_qq.isdigit():
                # 非数字会写进 qq_id，直接破坏该玩家后续的 QQ 鉴权
                yield event.plain_result(f"新 QQ 号必须是纯数字，收到: {new_qq}")
                return

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
            yield event.plain_result(PERMISSION_DENIED["god_only"])
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
            yield event.plain_result(PLAYER_NOT_FOUND.format(name=target_name))
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
            yield event.plain_result(PERMISSION_DENIED["god_only"])
            return

        # Cooldown（先检查，参数校验通过后才占用，避免写错格式就烧掉 600 秒冷却）
        cooldown_seconds = self.config.get("ladder_cooldown_seconds", 600)
        user_id = str(event.get_sender_id())
        cd_key = f"{user_id}:oath"
        if not self.cooldown_manager.check_cooldown(cd_key, cooldown_seconds):
            remaining = self.cooldown_manager.get_remaining(cd_key, cooldown_seconds)
            yield event.plain_result(OATH_COOLDOWN_MSG.format(seconds=f"{remaining:.0f}"))
            return

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

        self.cooldown_manager.set_cooldown(cd_key)
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
            yield event.plain_result(PERMISSION_DENIED["god_only"])
            return

        # Cooldown（参数校验通过后才占用）
        cooldown_seconds = self.config.get("ladder_cooldown_seconds", 600)
        cd_key = f"{user_id}:oath"
        if not self.cooldown_manager.check_cooldown(cd_key, cooldown_seconds):
            remaining = self.cooldown_manager.get_remaining(cd_key, cooldown_seconds)
            yield event.plain_result(OATH_COOLDOWN_MSG.format(seconds=f"{remaining:.0f}"))
            return

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

        self.cooldown_manager.set_cooldown(cd_key)
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
            max_name_len = self.config.get("player_name_max_length", 20)
            if len(new_name) > max_name_len:
                yield event.plain_result(f"玩家名过长，最长 {max_name_len} 个字符。")
                return
            success, message = await self.db_manager.rename_player_by_name(group_id, old_name, new_name)
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
            await self.db_manager.clear_oathbreaker(group_id, target_player.player_id)
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
            init_ladder = self.config.get("init_ladder_score", 1000)
            init_pilgrimage = self.config.get("init_pilgrimage_score", 100)
            await self.db_manager.update_scores(
                group_id, target_player.player_id,
                -target_player.ladder_score + init_ladder,
                -target_player.pilgrimage_score + init_pilgrimage,
                user_id, "管理员重置"
            )
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
            init_ladder = self.config.get("init_ladder_score", 1000)
            init_pilgrimage = self.config.get("init_pilgrimage_score", 100)
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

    # === 白名单 ===

    @filter.command("白名单", alias={"whitelist", "wl"})
    async def cmd_whitelist(self, event: AstrMessageEvent):
        """白名单管理。格式: 白名单 <add/remove/list/setfaith/removefaith> [用户ID] [信仰]"""
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

    # === 帮助 ===

    @filter.command("天梯榜帮助", alias={"ladderhelp"})
    async def cmd_help(self, event: AstrMessageEvent):
        """显示帮助信息"""
        text = format_help(dict(self.config))
        yield event.plain_result(text)

    # === QQ 群管命令（委托到 QQAdminHandler） ===

    @filter.command("禁言")
    async def cmd_w_ban(self, event: AstrMessageEvent):
        """禁言指令入口，逻辑委托给 QQAdminHandler.handle_ban。"""
        async for result in self._qq_admin.handle_ban(event):
            yield result

    @filter.command("解禁")
    async def cmd_w_unban(self, event: AstrMessageEvent):
        """解禁指令入口，逻辑委托给 QQAdminHandler.handle_unban。"""
        async for result in self._qq_admin.handle_unban(event):
            yield result

    @filter.command("踢人")
    async def cmd_w_kick(self, event: AstrMessageEvent):
        """踢人指令入口，逻辑委托给 QQAdminHandler.handle_kick。"""
        async for result in self._qq_admin.handle_kick(event):
            yield result

    @filter.command("撤回")
    async def cmd_w_recall(self, event: AstrMessageEvent):
        """撤回指令入口，逻辑委托给 QQAdminHandler.handle_recall。"""
        async for result in self._qq_admin.handle_recall(event):
            yield result

    @filter.command("全员禁")
    async def cmd_w_mute_all(self, event: AstrMessageEvent):
        """全员禁言入口，逻辑委托给 QQAdminHandler.handle_mute_all。"""
        async for result in self._qq_admin.handle_mute_all(event):
            yield result

    @filter.command("全员解")
    async def cmd_w_unmute_all(self, event: AstrMessageEvent):
        """解除全员禁言入口，逻辑委托给 QQAdminHandler.handle_unmute_all。"""
        async for result in self._qq_admin.handle_unmute_all(event):
            yield result

    @filter.command("设置精华")
    async def cmd_set_essence(self, event: AstrMessageEvent):
        """设置精华消息入口，逻辑委托给 QQAdminHandler.handle_set_essence。"""
        async for result in self._qq_admin.handle_set_essence(event):
            yield result

    @filter.command("移除精华")
    async def cmd_remove_essence(self, event: AstrMessageEvent):
        """移除精华消息入口，逻辑委托给 QQAdminHandler.handle_remove_essence。"""
        async for result in self._qq_admin.handle_remove_essence(event):
            yield result

    # === 储物空间 ===

    @filter.command("查询储物空间")
    async def cmd_query_inventory(self, event: AstrMessageEvent):
        """查看玩家储物空间。格式: 查询储物空间（查自己）或 查询储物空间 <玩家名> ...（诸神批量）"""
        async for result in self._query_inventory_impl(event):
            yield result

    def _parse_item_args(self, text: str) -> list:
        """解析道具参数，返回 [(道具名（可能含等级）, 数量), ...]。

        实现放在 item_utils.parse_item_args（纯函数，测试可直接调用；
        main.py 依赖 astrbot，测试环境导入不了）。这里只做转发，
        保持类内既有调用点不变。数量非正时抛 ValueError，由调用方转成提示。
        """
        return parse_item_args(text)

    @filter.command("赐予道具")
    async def cmd_give_item(self, event: AstrMessageEvent):
        """赐予道具。格式: 赐予道具 <玩家名> <道具1> <数量1> [道具2] [数量2] ...
        数量也可写作 道具*数量 或 道具×数量（位置不限，可写在等级括号前后）。"""
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

    @filter.command("收回道具")
    async def cmd_remove_item(self, event: AstrMessageEvent):
        """收回道具。格式: 收回道具 <玩家名> <道具*数量> 或 收回道具 <玩家名> <编号> ..."""
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
            yield event.plain_result(f"玩家 {player_name} 不存在")
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
                    items.append((item["item_name"], None))  # None = 全部收回
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

    @filter.command("清除储物空间")
    async def cmd_clear_inventory(self, event: AstrMessageEvent):
        """清除储物空间。格式: 清除储物空间 <玩家名> [道具名|全部]
        清空全部道具需要加「全部」确认，清除指定道具不需要。"""
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

    # === 白名单自动同步 ===

    @filter.command("同步白名单")
    async def cmd_sync_whitelist(self, event: AstrMessageEvent):
        """同步指定群的当前成员到白名单。格式: 同步白名单"""
        if not self._is_plugin_admin(event):
            yield event.plain_result(PERMISSION_DENIED["god_only"])
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
            success, _ = await self.permission_service.add_to_whitelist(uid, "sync")
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
            success, msg = await self.permission_service.add_to_whitelist(user_id, "auto")
            if success:
                logger.info(f"[AutoWhitelist] 自动添加白名单: {user_id}")
                self.permission_service.invalidate_cache(user_id)
        elif action == "leave":
            success, msg = await self.permission_service.remove_from_whitelist(user_id)
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
            yield event.plain_result(PERMISSION_DENIED["god_only"])
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

    @filter.command("清除状态")
    async def cmd_clear_status(self, event: AstrMessageEvent):
        """清除所有状态。格式: 清除状态 <玩家名>"""
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

    # === 赠送道具 ===

    @filter.command("赠送道具")
    async def cmd_gift_item(self, event: AstrMessageEvent):
        """赠送道具。格式: 赠送道具 <接收方名> <道具名> [数量]
        发送方由发送者 QQ 绑定鉴权（防名片冒充），接收方仍按玩家名查找。"""
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
        self._pending_gifts_receive[gift_key] = gift_data

        from astrbot_plugin_faith_ladder.message_formatter import format_gift_request
        notification = format_gift_request(
            sender_name, receiver_name, base_name, grade, quantity
        )
        yield event.plain_result(
            f"已从 {sender_name} 扣除，等待 {receiver_name} 接受。\n\n{notification}"
        )

    # === 接受道具 ===

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

        # 检查今日接受道具次数（可配置上限，0 为不限制）。诸神代收不受此上限约束。
        if not is_god:
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

        # 先认领（删除记录）再发放：并发下只有一方能删到，避免同一笔赠送被发放两次
        self._forget_pending_gift_cache(group_id, receiver_id)
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

    # === 拒绝道具 ===

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

        # 先认领（删除记录）再退回：并发下只有一方能删到，避免重复退款
        self._forget_pending_gift_cache(group_id, receiver_id)
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

    # ── 祷词触发 ──

    def _build_prayer_cache(self):
        """构建祷词缓存：{归一化祷词: 具体信仰名}。启动时和配置变更时调用。"""
        self._prayer_cache = {}
        # 祷词配置按具体信仰（faith）存储，16个信仰
        for faith in VALID_FAITHS:
            key = f"prayer_text_{faith}"
            prayers = self.config.get(key, [])
            for prayer in prayers:
                normalized = self._normalize_prayer_text(prayer)
                if normalized:
                    self._prayer_cache[normalized] = faith

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
        logger.debug(f"[PrayerTrigger] Player resolved: {player.player_name if player else None}, specific_faith: {player.specific_faith if player else None}")
        if not player:
            logger.debug("[PrayerTrigger] Player not found")
            return

        # 10. 只在字段缺失时才获取名片补全
        sender_id = str(event.get_sender_id())
        card = ""
        if not player.specific_faith or not player.class_:
            try:
                member_info = await event.bot.get_group_member_info(group_id=int(group_id), user_id=int(sender_id))
                card = member_info.get("card", "") or member_info.get("nickname", "")
            except Exception as e:
                logger.debug(f"[PrayerTrigger] Failed to get member info: {e}")

        # 补全具体信仰
        if not player.specific_faith and card:
            sf = self._extract_specific_faith(card)
            if sf:
                await self.db_manager.set_player_specific_faith(group_id, player.player_id, sf)
                # 同步内存对象：下面马上要用它判断"有无具体信仰"，
                # 不同步的话本次祷词会被当成无信仰直接丢弃，只能等下一次触发
                player.specific_faith = sf
                logger.info(f"[PrayerTrigger] 补全信仰: {player.player_name} ← {sf}")

        # 补全职业（从名片中提取职业关键词）
        if not player.class_ and card:
            for word in self._extract_card_words(card):
                if word in VALID_CLASSES:
                    await self.db_manager.set_player_class(group_id, player.player_id, word, player.faith or "")
                    logger.info(f"[PrayerTrigger] 补全职业: {player.player_name} ← {word}")
                    break
                # 检查具体职业
                for specific_name, (sf, sp, sc) in self._specific_classes.items():
                    if word == specific_name or word.startswith(specific_name):
                        await self.db_manager.set_player_class(group_id, player.player_id, sc, sp)
                        if not player.specific_faith:
                            await self.db_manager.set_player_specific_faith(group_id, player.player_id, sf)
                        logger.info(f"[PrayerTrigger] 补全职业: {player.player_name} ← {sc}（{sf}）")
                        break

        # 自动绑定 QQ
        if not player.qq_id:
            existing = await self.db_manager.get_player_by_qq(group_id, sender_id)
            if not existing:
                await self.db_manager.set_player_qq(group_id, player.player_id, sender_id)
                logger.info(f"[PrayerTrigger] 自动绑定 QQ: {player.player_name} ← {sender_id}")

        # 检查具体信仰是否补全成功
        if not player.specific_faith:
            logger.debug("[PrayerTrigger] Player has no specific faith, skipping")
            return

        # 11. 检查今日是否已触发（DB 查询）
        if await self.db_manager.has_prayer_hit_today(group_id, player.player_id):
            return

        # 12. 随机打分
        import random
        faith_matches = player.specific_faith == matched_faith
        if faith_matches:
            display_delta = random.randint(-2, 2)   # 匹配：-2 ~ +2
        else:
            display_delta = random.randint(-2, 0)   # 不匹配（渎神）：-2 ~ 0

        # 打分开关：默认关闭时只做氛围互动，展示随机结果但不改动实际分数
        score_enabled = self.config.get("prayer_score_enabled", False)
        db_delta = display_delta if score_enabled else 0

        # 13. 记录今日已触发（DB 写入本次实际生效的分值）
        recorded = await self.db_manager.record_prayer_hit(group_id, player.player_id, db_delta)
        if not recorded:
            return  # 并发情况，已被其他请求抢先

        # 14. 加分（仅开关打开且分值非 0 时）
        if db_delta != 0:
            ok, _ = await self.ladder_service.add_score(
                group_id, player.player_id, player.player_name,
                ladder_delta=0, pilgrimage_delta=db_delta,
                operator_id="prayer_trigger",
                reason="祷词触发"
            )
            if not ok:
                return

        # 15. 回复群消息 + 阻止 AI 也响应祷词（显示随机结果）
        msg = format_prayer_trigger(player.player_name, player.specific_faith, matched_faith, display_delta, self.config)
        yield event.plain_result(msg)
        event.stop_event()
