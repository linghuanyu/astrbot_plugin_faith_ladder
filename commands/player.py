"""
玩家档案与身份绑定（录入/检测/绑定/换绑/职业/立誓/弃誓）。

实现体所在模块：`@filter.command` 装饰器与指令注册保留在 main.py。
AstrBot 只扫描插件类自身的方法来注册指令，装饰器放进 mixin 会被沿 MRO
重复扫到，导致重复注册并中断后续指令的注册（约定见 commands/__init__.py）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent

from astrbot_plugin_faith_ladder.text_utils import (
    CQ_CODE_RE, looks_like_mention_tail, strip_mentions, to_halfwidth,
)

from astrbot_plugin_faith_ladder.messages import (
    OATH_COOLDOWN_MSG, PERMISSION_DENIED, PLAYER_NOT_FOUND,
)
from astrbot_plugin_faith_ladder.models import (
    FAITH_TO_PATH, VALID_CLASSES, VALID_FAITHS, VALID_PATHS,
)
try:
    from astrbot.api import logger
except ImportError:  # 无 AstrBot 环境（如跑测试）时退回标准库日志
    import logging
    logger = logging.getLogger(__name__)


class PlayerCommandsMixin:
    """玩家档案与身份绑定（录入/检测/绑定/换绑/职业/立誓/弃誓）。"""

    async def _register_player_impl(self, event: "AstrMessageEvent"):
        """录入新玩家。（注册在 main.py）"""
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

        args = self._get_args(event, "录入玩家")
        if not args:
            args = self._get_args(event, "register") or self._get_args(event, "添加玩家")

        # 检查是否有 @ 用户
        at_user_id = await self._get_at_user_id(event)
        auto_faith = None
        auto_class = None
        auto_name = None
        auto_specific_faith = None
        card_read_error = None

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
            except Exception as e:
                # 不再静默：取不到名片时下面只会报"缺少必要参数"，看不出真正原因
                # （机器人没有查看群成员的权限、对方已退群、适配器接口异常）
                card_read_error = e
                logger.warning(
                    f"[Register] 读取群成员信息失败 group={group_id} qq={at_user_id}: {e}"
                )

        # 从参数文本中提取显式值
        # args 来自 _get_args()，@ 提及已在那边整段剥掉：适配器把**整张名片**拼成
        # "@昵称(QQ)"，只按空白截断会漏下 "渔夫 1000 100(QQ)" 这种尾巴，"渔夫"
        # 会顶掉名片姓名、"100(QQ)" 会直接变成玩家名。这里再兜一次底（宿主换了
        # @ 文本形态时），下面还会把"@残尾"形状的名字候选丢掉。
        explicit_name = None
        explicit_faith = None
        explicit_class = None
        explicit_specific_faith = None
        scores = []

        clean_args = strip_mentions(args)

        parts = clean_args.split() if clean_args else []

        # 分类参数：数字→分数，具体职业名→职业（并带出信仰/命途），
        # 具体信仰→信仰（推出命途），命途→命途，基础职业→职业，其余→玩家名候选。
        #
        # 具体职业名与信仰名必须先于「玩家名」判定：名片上写的是具体职业
        # （混乱·猎人 → 渔夫），诸神照抄进参数时，此前会落进名字候选，
        # 又因为显式名优先于名片解析，被 @ 的人会静默录成一个叫「渔夫」的玩家
        # （职业/命途由名片补全，校验还能全过）。
        name_candidates = []

        for p in parts:
            try:
                # 用 int() 而非 isdigit()，以支持 +500 / -5 这类带符号分数；
                # 折半角是为了全角加减号（＋500 / －5）——int() 认不出全角符号
                scores.append(int(to_halfwidth(p)))
                continue
            except ValueError:
                pass
            specific = self._specific_classes.get(p)
            if specific:
                # (具体信仰, 命途, 基础职业)
                explicit_class = specific[2]
                if explicit_specific_faith is None:
                    explicit_specific_faith = specific[0]
                if explicit_faith is None:
                    explicit_faith = specific[1]
            elif p in VALID_FAITHS:
                if explicit_specific_faith is None:
                    explicit_specific_faith = p
                if explicit_faith is None:
                    explicit_faith = FAITH_TO_PATH.get(p)
            elif p in VALID_PATHS:
                explicit_faith = p
            elif p in VALID_CLASSES:
                explicit_class = p
            else:
                # 具体职业简称兜底（「子嗣牧」→「子嗣牧师」）：必须排在信仰/命途/
                # 基础职业之后——「生命」既是命途、也是「生命贤者」的前缀，
                # 得先按命途算（名片上写【生命】的本意就是命途）。
                # 认了简称就不再落进名字候选（那正是「渔夫」占掉名字槽的事故形态）。
                abbreviation = self._specific_class_prefixes.get(p)
                if abbreviation:
                    explicit_class = abbreviation[2]
                    if explicit_specific_faith is None:
                        explicit_specific_faith = abbreviation[0]
                    if explicit_faith is None:
                        explicit_faith = abbreviation[1]
                else:
                    name_candidates.append(p)

        # 非数字/信仰/职业的词，取第一个**不是 @ 残尾**的作为玩家名。
        # "X(本消息某个@的QQ)" 一定是 @ 文本没剥干净留下的（见 text_utils.strip_mentions），
        # 把它当名字就会录出一个假玩家（线上真出现过「100(2921544554)」这种名字）。
        at_qqs = [qq for _name, qq in self._mention_texts(event)]
        for candidate in name_candidates:
            if looks_like_mention_tail(candidate, at_qqs):
                logger.warning(f"[Register] 丢弃疑似 @ 残尾的名字候选: {candidate}")
                continue
            explicit_name = candidate
            break

        # 合并：显式 > 自动提取
        player_name = explicit_name or auto_name
        faith_name = explicit_faith or auto_faith
        class_name = explicit_class or auto_class
        specific_faith = explicit_specific_faith or auto_specific_faith

        # 校验必填项
        errors = []
        if not player_name:
            errors.append("玩家名（未从名片识别到，请在参数中指定）")
        if not faith_name:
            errors.append(f"命途（可选: {'/'.join(VALID_PATHS)}）")
        if not class_name:
            errors.append(f"职业（可选: {'/'.join(VALID_CLASSES)}）")

        if errors:
            # 「补一次即可」示例：已知的照原样填上，缺的留 <占位>，让诸神能直接照抄
            # （此前只列缺失项与可选值，补写时还得自己拼命令）。
            sample_tokens = []
            if at_user_id:
                # @ 录入时名字默认取名片，缺了才要补
                sample_tokens.append("@用户")
                if not player_name:
                    sample_tokens.append("<姓名>")
            else:
                sample_tokens.append(player_name or "<姓名>")
            if not faith_name:
                sample_tokens.append("<命途>")
            if not class_name:
                sample_tokens.append("<职业>")
            sample = "录入玩家 " + " ".join(sample_tokens)

            auto_info = ""
            if at_user_id:
                if card_read_error is not None:
                    auto_info = (
                        f"\n未能读取被 @ 用户的群名片（{card_read_error}）："
                        "可能是机器人没有查看群成员的权限，或对方已不在群内。"
                    )
                else:
                    auto_info = (
                        f"\n从名片自动提取: 姓名={auto_name or '?'}, "
                        f"信仰={auto_specific_faith or auto_faith or '?'}, "
                        f"职业={auto_class or '?'}"
                    )
            yield event.plain_result(
                f"缺少必要参数: {', '.join(errors)}\n"
                f"补一次即可（把 <…> 换成实际值）：{sample}\n"
                f"完整用法：录入玩家 @用户 [姓名] [命途] [职业] [登神之路分] [觐见分]\n"
                f"命途与职业也可写成具体信仰名（如 命运）或具体职业名（如 子嗣牧师）{auto_info}"
            )
            return

        max_name_len = self._cfg("player_name_max_length")
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
            pilgrimage_score = self._cfg("init_pilgrimage_score")
        else:
            ladder_score = self._cfg("init_ladder_score")
            pilgrimage_score = self._cfg("init_pilgrimage_score")

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
            # 具体信仰：参数里写了具体职业名/信仰名就用它，否则用名片解析出的
            specific_faith=specific_faith,
        )

        # 回复统一：注册结果（玩家名/职业/信仰/分数/信仰文案）两条路径都要给出，
        # 成功时尽量 @ 到被录入的人 —— @ 录入用 @ 的 QQ（同时绑定），无 @ 录入时
        # 按玩家名在群成员里找唯一匹配（找不到/有歧义就不 @，退回纯文本）。
        # 曾在此拼过「（对应群名片：X，QQ: Y）」与「已自动绑定你的 QQ」，按需求去掉；
        # 后者依赖的"祷词确认后取消录入"机制早已移除，那句后果本就不成立。
        notify_qq = at_user_id
        if success and not notify_qq:
            member = await self._find_member_by_name(event, player_name)
            if member:
                notify_qq = str(member.get("user_id") or "")

        if success and notify_qq and notify_qq.isdigit():
            from astrbot.core.message.components import At, Plain
            yield event.chain_result([
                At(qq=int(notify_qq)),
                Plain(text=f" {message}")
            ])
        else:
            yield event.plain_result(message)

    async def _register_faith_impl(self, event: "AstrMessageEvent"):
        """凭自己的群名片自助录入。（注册在 main.py）

        与「录入玩家 @用户」共用同一套名片解析与落库路径，区别只在：
        - 不需要诸神权限：名片由发送者自己维护，录入出的记录也绑定发送者自己的 QQ
        - 不接受参数、分数只取初始值：否则任何人都能给自己填任意分数
        - 已有记录一律不接管：弱身份（群名片）不能产生换绑/抢占这类持久副作用
        """
        blocked, gate_msg = await self._gate(event, None)
        if blocked:
            if gate_msg:
                yield event.plain_result(gate_msg)
            return

        group_id = self._get_group_id(event)
        user_id = str(event.get_sender_id())
        if not user_id.isdigit():
            yield event.plain_result("无法确认你的 QQ 号，信仰注册需在 QQ 群内使用。")
            return

        # 参数一律不认：分数若可自填就失去意义，姓名/命途/职业也只认群名片
        if any(self._get_args(event, name) for name in ("信仰注册", "faithregister", "自助注册")):
            yield event.plain_result(
                "信仰注册只认群名片，不接受参数。\n"
                "请把 姓名 / 命途（或具体信仰）/ 职业（或具体职业）写进群名片后再发送，"
                "例如：【繁荣】张三 战士"
            )
            return

        # 一个 QQ 在同一群只能绑定一个玩家：已经登记过的人不必（也不能）再注册
        existing_qq = await self.db_manager.get_player_by_qq(group_id, user_id)
        if existing_qq:
            yield event.plain_result(
                f"你的 QQ 已绑定玩家「{existing_qq.player_name}」，无需重复注册。\n"
                "如需改名或换绑，请联系诸神。"
            )
            return

        try:
            member_info = await event.bot.get_group_member_info(
                group_id=int(group_id), user_id=int(user_id)
            )
            card = member_info.get("card") or member_info.get("nickname") or ""
        except Exception as e:
            # 与录入玩家一致：取不到名片要说清原因，否则玩家只会看到"格式不符"
            logger.warning(
                f"[SelfRegister] 读取群成员信息失败 group={group_id} qq={user_id}: {e}"
            )
            yield event.plain_result(
                f"未能读取你的群名片（{e}）。\n"
                "可能是机器人没有查看群成员的权限，或你的账号信息暂时不可用；"
                "请稍后再试，或请诸神用「录入玩家 @你」代为录入。"
            )
            return

        if not card.strip():
            yield event.plain_result(
                "你的群名片是空的，无法识别。请先设置群名片"
                "（姓名 / 命途 / 职业）后再发送「信仰注册」。"
            )
            return

        parsed = self._parse_card_info(card)
        player_name = parsed["player_name"]
        faith_name = parsed["faith"]
        class_name = parsed["class_"]
        specific_faith = parsed["specific_faith"]

        missing = []
        if not player_name:
            missing.append("姓名")
        if not faith_name:
            missing.append("命途（或具体信仰）")
        if not class_name:
            missing.append("职业（或具体职业）")
        if missing:
            yield event.plain_result(
                f"群名片「{card}」不符合录入格式，缺少: {', '.join(missing)}\n"
                "格式示例：【繁荣】张三 战士（具体信仰 + 姓名 + 职业）\n"
                "           张三 生命 战士（姓名 + 命途 + 基础职业）\n"
                f"当前识别: 姓名={player_name or '?'}, "
                f"信仰={specific_faith or faith_name or '?'}, "
                f"职业={class_name or '?'}\n"
                "改好群名片后再发送「信仰注册」；也可请诸神用「录入玩家 @你」代为录入。"
            )
            return

        max_name_len = self._cfg("player_name_max_length")
        if len(player_name) > max_name_len:
            yield event.plain_result(
                f"名片里的玩家名过长（{len(player_name)} 字），最长 {max_name_len} 个字符。"
            )
            return

        existing = await self.db_manager.get_player_by_name(group_id, player_name)
        if existing:
            if existing.qq_id == user_id:
                would_be = "你的 QQ 已绑定该玩家，无需重复注册。"
            elif existing.qq_id:
                would_be = (
                    "该玩家已被其他 QQ 绑定，信仰注册不会接管已有记录。\n"
                    "请把群名片改成与你本人对应的姓名，或联系诸神处理。"
                )
            else:
                would_be = (
                    "该玩家尚未绑定 QQ。为防冒充，信仰注册不会接管已有记录；"
                    "请联系诸神用「绑定QQ」处理。"
                )
            yield event.plain_result(f"玩家「{player_name}」已在本宇宙中。{would_be}")
            return

        success, message = await self.ladder_service.register_player(
            group_id, player_name, faith_name, class_name,
            self._cfg("init_ladder_score"), self._cfg("init_pilgrimage_score"),
            user_id,
            qq_id=user_id,
            specific_faith=specific_faith,
        )
        if not success:
            yield event.plain_result(message)
            return

        logger.info(
            f"[SelfRegister] 自助录入成功 group={group_id} qq={user_id} "
            f"name={player_name} faith={faith_name} class={class_name}"
        )
        from astrbot.core.message.components import At, Plain
        yield event.chain_result([
            At(qq=int(user_id)),
            Plain(text=f" {message}"),
        ])

    async def _bind_qq_impl(self, event: "AstrMessageEvent"):
        """为指定玩家绑定 QQ（诸神权限）。（注册在 main.py）"""
        blocked, gate_msg = await self._gate(event, None)
        if blocked:
            if gate_msg:
                yield event.plain_result(gate_msg)
            return
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
        cleaned = CQ_CODE_RE.sub('', args).strip() if args else ""
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

    async def _rebind_qq_impl(self, event: "AstrMessageEvent"):
        """为玩家换绑 QQ（诸神权限）。（注册在 main.py）"""
        blocked, gate_msg = await self._gate(event, None)
        if blocked:
            if gate_msg:
                yield event.plain_result(gate_msg)
            return
        group_id = self._get_group_id(event)
        if not await self._check_perm(event):
            yield event.plain_result(PERMISSION_DENIED["god_only"])
            return

        args = self._get_args(event, "换绑QQ")
        if not args:
            # 别名调用同样要取到参数（此前用 rebindqq 时参数为空 → 每次都只回用法）
            args = self._get_args(event, "rebindqq") or self._get_args(event, "换绑qq")
        cleaned = CQ_CODE_RE.sub('', args).strip() if args else ""
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
                yield event.plain_result(f"在本宇宙未寻找到（{parts[0]}）")
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
                yield event.plain_result(f"在本宇宙未寻找到（{parts[0]}）")
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

    async def _set_class_impl(self, event: "AstrMessageEvent"):
        """修改玩家职业。（注册在 main.py）"""
        blocked, gate_msg = await self._gate(event, None)
        if blocked:
            if gate_msg:
                yield event.plain_result(gate_msg)
            return
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

    async def _take_oath_impl(self, event: "AstrMessageEvent"):
        """设置信仰。（注册在 main.py）"""
        blocked, gate_msg = await self._gate(event, None)
        if blocked:
            if gate_msg:
                yield event.plain_result(gate_msg)
            return
        group_id = self._get_group_id(event)

        if not await self._check_perm(event):
            yield event.plain_result(PERMISSION_DENIED["god_only"])
            return

        # Cooldown（先检查，参数校验通过后才占用，避免写错格式就烧掉 600 秒冷却）
        cooldown_seconds = self._cfg("ladder_cooldown_seconds")
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

        target_name, faith_name = parts
        # 冷却只在实际执行成功后才占用：服务层可能因"玩家不存在/命途非法"而失败，
        # 那种情况下不该让用户白白等 600 秒
        success, message = await self.ladder_service.set_faith(group_id, target_name, faith_name)
        if success:
            self.cooldown_manager.set_cooldown(cd_key)
        yield event.plain_result(message)

    async def _abandon_oath_impl(self, event: "AstrMessageEvent"):
        """标记弃誓者。（注册在 main.py）"""
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

        # Cooldown（参数校验通过后才占用）
        cooldown_seconds = self._cfg("ladder_cooldown_seconds")
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

        target_name = parts[0]
        new_faith = parts[1] if len(parts) > 1 else None
        success, message = await self.ladder_service.abandon_oath(
            group_id, target_name, new_faith, dict(self.config)
        )
        if success:  # 同上：失败不该消耗冷却
            self.cooldown_manager.set_cooldown(cd_key)
        yield event.plain_result(message)
