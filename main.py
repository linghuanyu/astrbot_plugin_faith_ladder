"""
信仰游戏天梯排行榜 - Faith Game Ladder Plugin for AstrBot
A dual-ladder ranking system with class/faith customization for group chats.
"""

import sys
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

try:
    from astrbot_plugin_faith_ladder.db_manager import DatabaseManager
    from astrbot_plugin_faith_ladder.ladder_service import LadderService
    from astrbot_plugin_faith_ladder.permission_service import PermissionService
    from astrbot_plugin_faith_ladder.cooldown import CooldownManager
    from astrbot_plugin_faith_ladder.models import FAITH_TO_PATH, Player
    from astrbot_plugin_faith_ladder.item_utils import parse_item_args
    from astrbot_plugin_faith_ladder import card_utils
    from astrbot_plugin_faith_ladder.text_utils import strip_mentions
    from astrbot_plugin_faith_ladder.qq_admin_handle import QQAdminHandler
    from astrbot_plugin_faith_ladder.faith_messages import FAITH_MESSAGES, GENERIC_GOD_MESSAGES
    from astrbot_plugin_faith_ladder.wish_service import WishService
    from astrbot_plugin_faith_ladder.commands import (
        QueryCommandsMixin,
        ScoreboardCommandsMixin,
        ScoreCommandsMixin,
        PlayerCommandsMixin,
        InventoryCommandsMixin,
        GiftCommandsMixin,
        AdminCommandsMixin,
        PrayerCommandsMixin,
        WishCommandsMixin,
        SharedSendMixin,
        ConfigMixin,
        GateMixin,
        WagerMixin,
    )
except ImportError as e:
    # 逐文件上传（只传新增文件、漏了被改的老文件）会让包内导入失败，而日志里只有一句
    # 底层 traceback，看不出这是「文件不完整」还是「代码坏了」——现场排查时就得靠猜。
    # 这里补一条能直接定位的说明，然后**原样抛出**：绝不吞异常。
    # 带着一半的代码继续加载比加载失败更糟（本仓库有过异常被框架吞掉、调度器静默
    # 停摆数个版本的先例）。
    logger.error(
        "[FaithLadder] 插件文件不完整或版本混杂：包内导入失败 —— "
        f"{e.msg or e}\n"
        "最常见的原因是逐文件上传时只传了新增文件、漏了被修改的老文件；"
        "本版必须成套更新（尤其 db_manager.py、commands/ 与 _conf_schema.json）。\n"
        "请用 git 拉取完整版本，或用整目录覆盖安装（不要只覆盖新增文件）。"
    )
    raise




@register(
    "astrbot_plugin_faith_ladder",
    "custom",
    "双积分排名插件，登神之路+觐见之梯双榜展示，支持弃誓/立誓系统、批量录入、道具储物空间与赠送、祈愿试炼组队、QQ群管指令，文案取《诸神愚戏》原文用词，适用于社群活动积分管理。仅支持群聊使用。",
    "3.9.0"
)
class FaithLadderPlugin(
    ScoreboardCommandsMixin,
    ScoreCommandsMixin,
    PlayerCommandsMixin,
    InventoryCommandsMixin,
    GiftCommandsMixin,
    AdminCommandsMixin,
    PrayerCommandsMixin,
    WishCommandsMixin,
    QueryCommandsMixin,
    SharedSendMixin,
    ConfigMixin,
    GateMixin,
    WagerMixin,
    Star,
):
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
        # 榜单缓存时长走配置（WebUI 改完立即生效；0 = 不缓存）
        self.ladder_service = LadderService(
            self.db_manager,
            ttl_getter=lambda: self._cfg("leaderboard_cache_seconds"),
            config_getter=lambda: dict(self.config),
        )
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
        # 祈愿试炼（组队）：队名/名额/日期口径都在服务里，这里只注入 DB 与配置读取器
        self.wish_service = WishService(
            self.db_manager,
            get_config=lambda: dict(self.config),
        )
        self._qq_admin = QQAdminHandler(
            check_perm_fn=self.permission_service.check_score_permission,
            # 群管指令本质是群务：超管或该群的群主/群管理员都可以执行。
            # 这不等于插件管理权——白名单/清空/重置走 _is_super_admin，群角色拿不到。
            check_admin_fn=self._is_group_staff,
            get_faith_fn=self._get_god_faith,
            # 群访问控制/功能开关与插件侧共用同一个闸门
            gate_fn=self._gate,
        )

        # 祷词触发缓存
        self._wagers: dict = {}       # group_id -> 进行中的赌局
        self._group_umos: dict = {}   # group_id -> 会话标识（定时任务发消息要用完整 umo）
        self._wager_last: dict = {}   # group_id -> 上次开局时间（单调时钟）
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
        except Exception as e:
            # 下面还有两级回退，这里只记 debug：单看这一条不能说明数据目录不对，
            # 但排查"数据存到别处去了"时需要知道第一级为什么没生效。
            logger.debug(f"[DataDir] 取 AstrBot 数据路径失败，尝试下一级回退: {e}")

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

    def _warn_deprecated_config_whitelist(self):
        """WebUI 的 whitelist 配置已废弃：残留非空时提醒一次，且不迁移。

        不提醒的话，"配置里明明配着人、权限却不生效"会被当成 bug 来排查——
        白名单现在只存在 DB 里。
        """
        stale = self._cfg("whitelist")
        if stale:
            logger.warning(
                f"[Whitelist] WebUI 的 whitelist 配置已废弃，其中 {len(stale)} 条不会生效"
                f"（也不迁移）；请改用「白名单 add <QQ> [信仰]」管理诸神"
            )

    async def initialize(self):
        """插件加载：建库与迁移、启动调度器、注册群成员变动监听。"""
        await self.db_manager.initialize()
        self._warn_deprecated_config_whitelist()
        from astrbot_plugin_faith_ladder.scheduler_service import SchedulerService

        async def send_to_group(group_id: str, content):
            """Send content to group. Content can be:
            - str: plain text
            - tuple ('image', bytes): image from bytes
            """
            try:
                umo = self._resolve_umo(group_id)
                if not umo:
                    logger.warning(f"未知会话标识，跳过向群 {group_id} 发送通知")
                    return
                from astrbot_plugin_faith_ladder.commands.shared import wrap_message_chain
                if isinstance(content, tuple) and len(content) == 2 and content[0] == "image":
                    from astrbot.api.message_components import Image
                    chain = wrap_message_chain([Image.fromBytes(content[1])])
                else:
                    from astrbot.api.message_components import Plain
                    text = content if isinstance(content, str) else str(content)
                    chain = wrap_message_chain([Plain(text=text)])
                await self.context.send_message(umo, chain)
            except Exception as e:
                logger.error(f"Failed to send to group {group_id}: {e}")

        self._scheduler = SchedulerService(
            data_dir=self.data_dir,
            get_config=lambda: dict(self.config),
            purge_score_history=self.db_manager.purge_old_score_history,
            purge_daily_tables=self.db_manager.purge_daily_tables,
            purge_expired_statuses=self.db_manager.purge_expired_statuses,
            purge_old_wish_teams=self.db_manager.purge_old_wish_teams,
            cleanup_expired_gifts=self.ladder_service.cleanup_expired_gifts,
            notify_gift_timeout=send_to_group,
            backup_db=self.db_manager.backup_to,
            wager_tick=self._wager_tick,
            wish_tick=self._wish_tick,
        )
        await self._scheduler.start()

        # 注册群成员变动监听（白名单自动同步 + 祈愿试炼的退群移出）
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
        """取事件所属群号（字符串）。插件仅支持群聊，私聊场景会取不到 group_id。

        顺手记下该群的会话标识：定时任务（赌局开局/开奖、赠送超时通知）手里没有
        event，而发消息需要一个完整会话串。
        """
        group_id = str(event.message_obj.group_id)
        self._remember_umo(group_id, event)
        return group_id

    def _remember_umo(self, group_id: str, event) -> None:
        """记下该群的会话标识 umo（AstrBot v4 形如 `xiaoyu:GroupMessage:821721918`）。

        旧写法 `group:<群号>` 只有两段，v4 的 send_message 会直接报
        「不合法的 session 字符串: not enough values to unpack (expected 3, got 2)」——
        赌局开奖消息就是这样被丢掉的，所以这里必须在收到群消息时把真实 umo 存下来。
        """
        umo = getattr(event, "unified_msg_origin", None)
        if umo and group_id:
            self._group_umos[str(group_id)] = str(umo)

    def _resolve_umo(self, group_id: str) -> Optional[str]:
        """取该群的会话串。没见过这个群的消息时，用已知的平台前缀拼一个。"""
        cached = self._group_umos.get(str(group_id))
        if cached:
            return cached
        for known in self._group_umos.values():
            prefix = str(known).rsplit(":", 1)[0]   # 形如 xiaoyu:GroupMessage
            if prefix:
                return f"{prefix}:{group_id}"
        return None

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
        return strip_mentions(text[len(cmd_name):])

    def _is_super_admin(self, event: AstrMessageEvent) -> bool:
        """插件超管：只认 config.admin_ids。

        与诸神权限是两套，也与群角色无关——群主/群管理员不因身份获得管理类操作
        （白名单、同步、清空、重置），他们的权限上限是「诸神级」。
        """
        return self.permission_service.is_admin(str(event.get_sender_id()))

    def _is_group_moderator(self, event: AstrMessageEvent) -> bool:
        """是否为该群的群主/群管理员（QQ 群角色）。"""
        try:
            if hasattr(event, 'message_obj') and hasattr(event.message_obj, 'sender'):
                return event.message_obj.sender.role in ('admin', 'owner')
        except (AttributeError, TypeError):
            pass
        return False

    def _is_group_staff(self, event: AstrMessageEvent) -> bool:
        """群管指令的执行者：超管，或该群的群主/群管理员。"""
        return self._is_super_admin(event) or self._is_group_moderator(event)

    async def _check_perm(self, event: AstrMessageEvent) -> bool:
        """诸神级判定：超管、DB 白名单诸神，或该群的群主/群管理员。

        check_score_permission 已含 admin_ids，故不在这里重复判超管。
        """
        if await self.permission_service.check_score_permission(str(event.get_sender_id())):
            return True
        return self._is_group_moderator(event)

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
        """宽松的身份解析：先查 QQ 绑定，失败则回退群名片识别（兼容未绑定的老玩家）。

        ⚠️ 回退得到的身份是**弱身份**——群名片由玩家自行修改。调用方不得基于这个
        结果制造持久化副作用（尤其不要绑定 QQ）：攻击者把名片改成他人名字，就能
        借用对方身份。绑定只应发生在强身份路径（录入玩家 @用户 / 绑定QQ）。
        当前调用方：查询类只读命令、祷词触发（仅计分，不绑定）。
        """
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



    # === 排行榜 ===

    @filter.command("天梯榜", alias={"ladder", "ranking", "排行榜"})
    async def cmd_ladder(self, event: AstrMessageEvent):
        """显示天梯排行榜（需要诸神权限）"""
        async for result in self._ladder_impl(event):
            yield result

    # === 觐见榜 ===

    @filter.command("觐见榜", alias={"pilgrimage", "觐见"})
    async def cmd_pilgrimage(self, event: AstrMessageEvent):
        """显示觐见之梯排行榜（需要诸神权限）"""
        async for result in self._pilgrimage_impl(event):
            yield result

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
        except Exception as e:
            # 取群名片失败（机器人不在群、无权限、协议端异常）会让"自动识别自己"
            # 直接失效，用户只看到一句"请指定玩家名"。此前这里是 pass，
            # 排查时完全看不出是没读到名片还是名片里确实没有匹配的名字。
            logger.warning(f"[ResolvePlayer] 读取群名片失败，自动识别不可用: {e}")
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
        """解析诸神/管理员指定的目标玩家名，返回 (player_name, rest_args, error)。

        这里**只处理诸神路径**：唯一调用点（非诸神查询）位于调用方的权限分支内，
        原"非诸神查自己"的 else 分支永远不可达（方法名里的 or_self 是历史遗留）。
        该分支已删除，同时省掉一次重复的权限查询。
        """
        target, rest = await self._parse_target_name(event, args)
        if not target:
            return None, "", "请指定玩家名。"
        return target, rest, None

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
        except Exception as e:
            # 群成员列表取不到（无权限 / 协议端不支持）时，@ 录入会退化成按名字录入；
            # 没有这行日志的话，"@ 了人却提示玩家不存在"会被当成名字写错。
            logger.warning(f"[FindMember] 取群成员列表失败，无法按名片定位 @ 的成员: {e}")
        return None

    def _extract_card_words(self, card: str) -> list:
        """从群名片中提取所有非纯数字的词（用于匹配）。实现见 card_utils。"""
        return card_utils.extract_card_words(card)

    def _extract_specific_faith(self, card: str) -> Optional[str]:
        """从【】标签中提取具体信仰名；非有效信仰返回 None。实现见 card_utils。"""
        return card_utils.extract_specific_faith(card)

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
        except Exception as e:
            # 取不到 @ 的 QQ 时，@ 类指令会退化成"未指定目标"或按名字解析；
            # 常见原因是宿主版本没有 At 组件或消息段结构不同，值得留痕。
            logger.warning(f"[AtUser] 解析 @ 用户失败，将按玩家名处理: {e}")
        return None

    def _parse_card_info(self, card: str) -> dict:
        """解析群名片，提取具体信仰/命途/职业/玩家名。实现见 card_utils。"""
        return card_utils.parse_card_info(card, self._sorted_specific_classes)

    @filter.command("查询", alias={"query", "查看"})
    async def cmd_query(self, event: AstrMessageEvent):
        """查询玩家信息。格式: 查询（自动识别自己）或 查询 <玩家名>（诸神指定）或 查询 @用户（诸神专用）或 查询 <玩家名1> <玩家名2> ...（诸神批量查询）"""
        async for result in self._query_impl(event):
            yield result

    # === 录入积分 ===

    @filter.command("录入积分", alias={"addscore", "加分"})
    async def cmd_add_score(self, event: AstrMessageEvent):
        """录入积分变化。格式: 录入积分 <玩家名> <天梯分变化> <觐见梯变化>"""
        async for result in self._add_score_impl(event):
            yield result

    # === 批量录入积分 ===

    @filter.command("批量录入", alias={"batch", "bl"})
    async def cmd_batch_add_score(self, event: AstrMessageEvent):
        """批量录入积分。格式: 批量录入 后粘贴结算文本"""
        async for result in self._batch_add_score_impl(event):
            yield result


    # === 录入玩家 ===

    @filter.command("录入玩家", alias={"register", "添加玩家"})
    async def cmd_register_player(self, event: AstrMessageEvent):
        """录入新玩家。格式:
        录入玩家 @用户 [姓名] [命途] [职业] [登神之路分] [觐见分]
          - @用户时自动从名片提取信仰/职业/姓名，显式参数可覆盖
          - 命途/职业也可写具体信仰名（繁荣）或具体职业名（渔夫）
        录入玩家 <姓名> <命途> <职业> [登神之路分] [觐见分]
          - 传统方式，手动指定所有参数
        """
        async for result in self._register_player_impl(event):
            yield result

    # === 检测玩家 ===

    @filter.command("检测玩家", alias={"check", "检测"})
    async def cmd_check_player(self, event: AstrMessageEvent):
        """检测当前玩家的绑定状态（QQ、信仰等），未绑定时自动绑定。"""
        async for result in self._check_player_impl(event):
            yield result

    # === 绑定 QQ ===

    @filter.command("绑定QQ", alias={"bindqq", "绑定qq"})
    async def cmd_bind_qq(self, event: AstrMessageEvent):
        """为指定玩家绑定 QQ（诸神权限）。
        格式：绑定QQ @用户 或 绑定QQ <玩家名>
        一个 QQ 在同一群只能绑定一个玩家。"""
        async for result in self._bind_qq_impl(event):
            yield result

    # === 换绑 QQ ===

    @filter.command("换绑QQ", alias={"rebindqq", "换绑qq"})
    async def cmd_rebind_qq(self, event: AstrMessageEvent):
        """为玩家换绑 QQ（诸神权限）。
        格式：
          换绑QQ @新QQ所属用户 <玩家名>    — 把玩家绑到 @ 用户的 QQ
          换绑QQ <玩家名> <新QQ号>          — 直接指定新 QQ 号
        若新 QQ 已绑其他玩家，提示先解绑/换绑。"""
        async for result in self._rebind_qq_impl(event):
            yield result

    # === 设置职业（仅职业） ===

    @filter.command("设置职业", alias={"setclass", "改职业"})
    async def cmd_set_class(self, event: AstrMessageEvent):
        """修改玩家职业。格式: 设置职业 <玩家名> <职业>"""
        async for result in self._set_class_impl(event):
            yield result

    # === 立誓（设置信仰） ===

    @filter.command("立誓", alias={"takeoath", "立约"})
    async def cmd_take_oath(self, event: AstrMessageEvent):
        """设置信仰。格式: 立誓 <玩家名> <信仰>"""
        async for result in self._take_oath_impl(event):
            yield result

    # === 弃誓 ===

    @filter.command("弃誓", alias={"abandoath"})
    async def cmd_abandon_oath(self, event: AstrMessageEvent):
        """标记弃誓者。格式: 弃誓 <玩家名> [新信仰]"""
        async for result in self._abandon_oath_impl(event):
            yield result

    # === 天梯榜管理 ===

    @filter.command("天梯榜管理", alias={"ladderadmin", "榜管理"})
    async def cmd_admin(self, event: AstrMessageEvent):
        """管理员/诸神操作。格式: 天梯榜管理 <操作> [参数]"""
        async for result in self._admin_impl(event):
            yield result

    # === 白名单 ===

    @filter.command("白名单", alias={"whitelist", "wl"})
    async def cmd_whitelist(self, event: AstrMessageEvent):
        """白名单管理。格式: 白名单 <add/remove/list/setfaith/removefaith> [用户ID] [信仰]"""
        async for result in self._whitelist_impl(event):
            yield result

    # === 帮助 ===

    @filter.command("天梯榜帮助", alias={"ladderhelp"})
    async def cmd_help(self, event: AstrMessageEvent):
        """显示帮助信息"""
        async for result in self._help_impl(event):
            yield result

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
        async for result in self._give_item_impl(event):
            yield result

    @filter.command("收回道具")
    async def cmd_remove_item(self, event: AstrMessageEvent):
        """收回道具。格式: 收回道具 <玩家名> <道具*数量> 或 收回道具 <玩家名> <编号> ..."""
        async for result in self._remove_item_impl(event):
            yield result

    @filter.command("清除储物空间")
    async def cmd_clear_inventory(self, event: AstrMessageEvent):
        """清除储物空间。格式: 清除储物空间 <玩家名> [道具名|全部]
        清空全部道具需要加「全部」确认，清除指定道具不需要。"""
        async for result in self._clear_inventory_impl(event):
            yield result

    # === 白名单自动同步 ===

    @filter.command("同步白名单")
    async def cmd_sync_whitelist(self, event: AstrMessageEvent):
        """同步指定群的当前成员到白名单。格式: 同步白名单"""
        async for result in self._sync_whitelist_impl(event):
            yield result


    async def on_group_member_change(self, event: AstrMessageEvent):
        """监听群成员变动事件，自动同步白名单。
        需要在 initialize() 中注册到事件总线。
        """
        return await self._group_member_change_impl(event)

    # === 状态 ===

    @filter.command("添加状态")
    async def cmd_add_status(self, event: AstrMessageEvent):
        """添加状态。格式: 添加状态 <玩家名> <状态名> <天数>"""
        async for result in self._add_status_impl(event):
            yield result

    @filter.command("移除状态")
    async def cmd_remove_status(self, event: AstrMessageEvent):
        """移除状态。格式: 移除状态 <玩家名> <状态名>"""
        async for result in self._remove_status_impl(event):
            yield result

    @filter.command("清除状态")
    async def cmd_clear_status(self, event: AstrMessageEvent):
        """清除所有状态。格式: 清除状态 <玩家名>"""
        async for result in self._clear_status_impl(event):
            yield result

    @filter.command("重命名状态")
    async def cmd_rename_status(self, event: AstrMessageEvent):
        """重命名状态。格式: 重命名状态 <玩家名> <旧状态名> <新状态名>"""
        async for result in self._rename_status_impl(event):
            yield result

    # === 神明的赌局 ===

    @filter.command("赌局", alias={"wager", "神明赌局"})
    async def cmd_wager(self, event: AstrMessageEvent):
        """手动抛下一场「神明的赌局」（需开启 wager_enabled；诸神/管理员）"""
        async for result in self._wager_open_impl(event):
            yield result

    # === 祈愿试炼（组队）===
    # 队名由系统生成（玩家不能自定义），满员自动发车，全员获得以队名命名的状态。

    @filter.command("祈愿", alias={"祈愿大厅"})
    async def cmd_wish(self, event: AstrMessageEvent):
        """祈愿大厅：本群招募中的队伍与当天的名额。"""
        async for result in self._wish_impl(event):
            yield result

    @filter.command("祈愿组队")
    async def cmd_wish_create(self, event: AstrMessageEvent):
        """开一支祈愿试炼的队伍（自动入座）。格式: 祈愿组队 [人数]"""
        async for result in self._wish_create_impl(event):
            yield result

    @filter.command("祈愿加入")
    async def cmd_wish_join(self, event: AstrMessageEvent):
        """加入队伍。格式: 祈愿加入 [队名]（省略队名则进最缺人的一支）"""
        async for result in self._wish_join_impl(event):
            yield result

    @filter.command("祈愿退出")
    async def cmd_wish_leave(self, event: AstrMessageEvent):
        """退出自己的队伍（队长退出即解散）。"""
        async for result in self._wish_leave_impl(event):
            yield result

    @filter.command("祈愿我的")
    async def cmd_wish_mine(self, event: AstrMessageEvent):
        """查看我的队伍与成员。"""
        async for result in self._wish_mine_impl(event):
            yield result

    @filter.command("祈愿随机")
    async def cmd_wish_random(self, event: AstrMessageEvent):
        """随机匹配：把自己补进最缺人的一支队伍。"""
        async for result in self._wish_random_impl(event):
            yield result

    @filter.command("祈愿管理")
    async def cmd_wish_admin(self, event: AstrMessageEvent):
        """祈愿试炼的诸神治理。格式: 祈愿管理 <操作> [参数]"""
        async for result in self._wish_admin_impl(event):
            yield result

    # === 赠送道具 ===

    @filter.command("赠送道具")
    async def cmd_gift_item(self, event: AstrMessageEvent):
        """赠送道具。格式: 赠送道具 <接收方名> <道具名> [数量]
        发送方由发送者 QQ 绑定鉴权（防名片冒充），接收方仍按玩家名查找。"""
        async for result in self._gift_item_impl(event):
            yield result

    # === 接受道具 ===

    @filter.command("接受道具")
    async def cmd_accept_gift(self, event: AstrMessageEvent):
        """接收方接受赠送，无需参数。诸神可带参数指定接收玩家（跳过名片检测）。"""
        async for result in self._accept_gift_impl(event):
            yield result

    # === 拒绝道具 ===

    @filter.command("拒绝道具")
    async def cmd_reject_gift(self, event: AstrMessageEvent):
        """接收方拒绝赠送，无需参数。诸神可带参数指定接收玩家（跳过 QQ 绑定检测）。"""
        async for result in self._reject_gift_impl(event):
            yield result

    # ── 祷词触发 ──





    @filter.regex(r".*")
    async def on_prayer_message(self, event: AstrMessageEvent, matched=None):
        """监听所有消息，检测祷词触发。"""
        async for result in self._prayer_message_impl(event):
            yield result
