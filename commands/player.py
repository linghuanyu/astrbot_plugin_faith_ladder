"""
玩家档案与身份绑定（录入/检测/绑定/换绑/职业/立誓/弃誓）。

实现体所在模块：`@filter.command` 装饰器与指令注册保留在 main.py。
AstrBot 只扫描插件类自身的方法来注册指令，装饰器放进 mixin 会被沿 MRO
重复扫到，导致重复注册并中断后续指令的注册（约定见 commands/__init__.py）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional, List, Dict, Tuple

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent

from astrbot_plugin_faith_ladder.text_utils import CQ_CODE_RE, AT_MENTION_RE

from astrbot_plugin_faith_ladder.messages import (
    OATH_COOLDOWN_MSG, PERMISSION_DENIED, PLAYER_NOT_FOUND,
)
from astrbot_plugin_faith_ladder.models import VALID_CLASSES, VALID_PATHS


class PlayerCommandsMixin:
    """玩家档案与身份绑定（录入/检测/绑定/换绑/职业/立誓/弃誓）。"""

    async def _register_player_impl(self, event: "AstrMessageEvent"):
        """录入新玩家。（注册在 main.py）"""
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

        clean_args = AT_MENTION_RE.sub('', args).strip()
        clean_args = CQ_CODE_RE.sub('', clean_args).strip()

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

    async def _check_player_impl(self, event: "AstrMessageEvent"):
        """检测当前玩家的绑定状态（QQ、信仰等），未绑定时自动绑定。（注册在 main.py）"""
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

    async def _bind_qq_impl(self, event: "AstrMessageEvent"):
        """为指定玩家绑定 QQ（诸神权限）。（注册在 main.py）"""
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

    async def _set_class_impl(self, event: "AstrMessageEvent"):
        """修改玩家职业。（注册在 main.py）"""
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

    async def _abandon_oath_impl(self, event: "AstrMessageEvent"):
        """标记弃誓者。（注册在 main.py）"""
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
