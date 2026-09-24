"""
祈愿试炼（组队）。

实现体所在模块：`@filter.command` 装饰器与指令注册保留在 main.py。
AstrBot 只扫描插件类自身的方法来注册指令，装饰器放进 mixin 会被沿 MRO
重复扫到，导致重复注册并中断后续指令的注册（约定见 commands/__init__.py）。

身份口径：这些指令会写入持久状态（成员状态），所以**一律走强身份**
（`_resolve_self_player`，即 QQ 绑定），不用群名片兜底——否则把名片改成别人的
玩家名就能替对方拿到状态。这与「赠送道具」一致。

发送口径：回执回给触发者，播报发给全群。播报的**策略**（群访问控制静默）放在
`_wish_broadcast`，**通道**放在 `_wish_send`（测试替换通道即可，不动策略）——
与赌局同构。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional, Tuple

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent

try:
    from astrbot.api import logger
except ImportError:  # 无 AstrBot 环境（如跑测试）时退回标准库日志
    import logging
    logger = logging.getLogger(__name__)

from astrbot_plugin_faith_ladder.messages import COOLDOWN_MSG, PERMISSION_DENIED
from astrbot_plugin_faith_ladder.text_utils import split_rename_pair

# 破坏性操作（清空）的二次确认口令，与「天梯榜管理 清空 确认」同一套约定
CONFIRM_TOKEN = "确认"

UNBOUND_MSG = (
    "你尚未绑定 QQ，无法参与祈愿试炼。\n"
    "请让诸神使用「绑定QQ @你」完成绑定。"
)

WISH_ADMIN_USAGE = (
    "用法：祈愿管理 <操作> [参数]\n"
    "  列表                              本群队伍（不含已解散）\n"
    "  已解散                            只看已解散的队伍\n"
    "  名单 <队名> [登神分] [觐见分]      队员名单；给分数则输出可直接粘贴进「批量录入」的文本\n"
    "  换人 <玩家1> <玩家2>              把玩家1 的房间状态转移到玩家2 身上\n"
    "  解散 <队名>                       解散队伍（已发车的会连带撤销全员状态）\n"
    "  重命名 <旧名> <新名>              改名（诸神是唯一的命名途径）\n"
    "  移出 <队名> <玩家名>              把成员移出并撤销其状态\n"
    "  补位 <队名> <玩家名>              强行把人塞进队伍（满员即发车）\n"
    "  延期 <队名> <天数>                该队成员状态从当前到期时间往后加\n"
    "  统计 [天数]                       近期开团/发车/参与与卡点（默认 7 天）\n"
    "  清空                             清空本群队伍记录并释放当天名额（需加「确认」）\n"
    "队名含空格时写在前面、玩家名放最后一段；改名两段都含空格时用 → 分隔。"
)


def parse_capacity(args: str) -> Tuple[Optional[int], Optional[str]]:
    """解析 `祈愿组队 [人数]`，返回 (人数, 错误提示)。

    玩家不能自定义队名，所以这里只接受一个可选的纯数字；写了别的会被当作用法错误，
    而不是静默忽略——静默忽略会让人以为队名生效了。
    """
    text = (args or "").strip()
    if not text:
        return None, None

    parts = text.split()
    if len(parts) != 1 or not parts[0].isdigit():
        return None, (
            "用法：祈愿组队 [人数]\n"
            "（队名由系统生成，不能自定义；例如：祈愿组队 4）"
        )
    return int(parts[0]), None


def split_tail_int(rest: str, count: int = 1) -> Tuple[str, List[int]]:
    """从末尾摘掉 count 个整数，剩下的当名字。返回 (名字, 整数列表)。

    「移出/补位/延期」都要在可能含空格的名字后面接参数，与「添加状态」从两端读的
    做法一致：名字从前往后、参数从后往前。
    """
    tokens = rest.split()
    numbers: List[int] = []
    while tokens and len(numbers) < count:
        try:
            numbers.insert(0, int(tokens[-1]))
        except ValueError:
            break
        tokens.pop()
    return " ".join(tokens), numbers


class WishCommandsMixin:
    """祈愿试炼（组队）。"""

    # ── 发送与门槛 ──

    def _wish_group_enabled(self, group_id: str) -> bool:
        """该群是否在 wish_groups 里（未列出 = 本群不开启）。"""
        groups = self._cfg("wish_groups") or []
        return str(group_id).strip() in {str(g).strip() for g in groups if str(g).strip()}

    @staticmethod
    def _wish_group_disabled_msg() -> str:
        return (
            "祈愿试炼未在本群开启。\n"
            "诸神可在插件配置的 wish_groups 里加上本群号。"
        )

    async def _wish_broadcast(self, group_id: str, texts) -> None:
        """播报的统一出口：被群访问控制排除的群一律不发。

        策略放在这里而不是 `_wish_send`，因为 `_wish_send` 是纯通道、测试会替换它——
        把策略藏进被替换的方法里就等于没测。
        """
        for text in texts or []:
            if not text:
                continue
            if self._group_access_blocked(group_id):
                continue
            await self._wish_send(group_id, text)

    async def _wish_send(self, group_id: str, text: str) -> None:
        """纯发送通道；无 context（单测）或会话串未知时静默跳过。"""
        context = getattr(self, "context", None)
        if context is None or not text:
            return
        umo = self._resolve_umo(group_id)
        if not umo:
            logger.warning(f"[Wish] 群 {group_id} 的会话标识未知，跳过播报")
            return
        try:
            from astrbot.api.message_components import Plain

            from astrbot_plugin_faith_ladder.commands.shared import wrap_message_chain
            await context.send_message(umo, wrap_message_chain([Plain(text=text)]))
        except Exception as e:
            logger.error(f"[Wish] 发送失败: {e}")

    def _wish_cooldown_seconds(self) -> int:
        try:
            return max(0, int(self._cfg("wish_cooldown_seconds")))
        except (TypeError, ValueError):
            return 0

    async def _wish_actor(self, event: "AstrMessageEvent"):
        """取发起者玩家（强身份）。取不到时直接给出提示文案。"""
        player = await self._resolve_self_player(event)
        if player is None:
            return None, UNBOUND_MSG
        return player, None

    def _wish_cooldown_block(self, event: "AstrMessageEvent", action: str) -> Optional[str]:
        """冷却判定；返回提示文案表示还在冷却中。"""
        seconds = self._wish_cooldown_seconds()
        if seconds <= 0:
            return None
        key = f"{event.get_sender_id()}:wish:{action}"
        if self.cooldown_manager.check_cooldown(key, seconds):
            return None
        remaining = self.cooldown_manager.get_remaining(key, seconds)
        return COOLDOWN_MSG.format(seconds=f"{remaining:.0f}")

    def _wish_start_cooldown(self, event: "AstrMessageEvent", action: str) -> None:
        seconds = self._wish_cooldown_seconds()
        if seconds > 0:
            self.cooldown_manager.set_cooldown(f"{event.get_sender_id()}:wish:{action}")

    async def _wish_after_gate(self, event, action: str, call):
        """闸门之后的公共骨架：群名单 → 冷却 → 强身份 → 调服务 → 播报 → 回执。

        闸门**不在这里**：每个 `*_impl` 必须自己第一行调 `self._gate(...)`（静态守卫
        会逐个体检查，见 tests/test_group_access.py）。`call` 是
        `async (group_id, player) -> WishOutcome`，各指令只提供这一小段。
        """
        group_id = self._get_group_id(event)
        if not self._wish_group_enabled(group_id):
            yield event.plain_result(self._wish_group_disabled_msg())
            return

        cooldown_msg = self._wish_cooldown_block(event, action)
        if cooldown_msg:
            yield event.plain_result(cooldown_msg)
            return

        player, error = await self._wish_actor(event)
        if error:
            yield event.plain_result(error)
            return

        self._wish_start_cooldown(event, action)
        outcome = await call(group_id, player)
        await self._wish_broadcast(group_id, outcome.broadcasts)
        yield event.plain_result(outcome.reply)

    # ── 玩家侧 ──

    async def _wish_impl(self, event: "AstrMessageEvent"):
        """祈愿大厅。（注册在 main.py）"""
        blocked, gate_msg = await self._gate(event, "wish")
        if blocked:
            if gate_msg:
                yield event.plain_result(gate_msg)
            return
        group_id = self._get_group_id(event)
        if not self._wish_group_enabled(group_id):
            yield event.plain_result(self._wish_group_disabled_msg())
            return

        outcome = await self.wish_service.list_open(group_id)
        yield event.plain_result(outcome.reply)

    async def _wish_create_impl(self, event: "AstrMessageEvent"):
        """祈愿组队 [人数]。（注册在 main.py）"""
        blocked, gate_msg = await self._gate(event, "wish")
        if blocked:
            if gate_msg:
                yield event.plain_result(gate_msg)
            return
        group_id = self._get_group_id(event)
        if not self._wish_group_enabled(group_id):
            yield event.plain_result(self._wish_group_disabled_msg())
            return

        capacity, error = parse_capacity(self._get_args(event, "祈愿组队"))
        if error:
            yield event.plain_result(error)
            return

        player, unbound = await self._wish_actor(event)
        if unbound:
            yield event.plain_result(unbound)
            return

        outcome = await self.wish_service.create(
            group_id, player.player_id, player.player_name, capacity
        )
        await self._wish_broadcast(group_id, outcome.broadcasts)
        yield event.plain_result(outcome.reply)

    async def _wish_join_impl(self, event: "AstrMessageEvent"):
        """祈愿加入 [队名]。（注册在 main.py）"""
        blocked, gate_msg = await self._gate(event, "wish")
        if blocked:
            if gate_msg:
                yield event.plain_result(gate_msg)
            return

        async def call(group_id, player):
            team_name = self._get_args(event, "祈愿加入").strip() or None
            return await self.wish_service.join(
                group_id, player.player_id, player.player_name, team_name
            )

        async for result in self._wish_after_gate(event, "join", call):
            yield result

    async def _wish_leave_impl(self, event: "AstrMessageEvent"):
        """祈愿退出。（注册在 main.py）"""
        blocked, gate_msg = await self._gate(event, "wish")
        if blocked:
            if gate_msg:
                yield event.plain_result(gate_msg)
            return

        async def call(group_id, player):
            return await self.wish_service.leave(group_id, player.player_id)

        async for result in self._wish_after_gate(event, "leave", call):
            yield result

    async def _wish_mine_impl(self, event: "AstrMessageEvent"):
        """祈愿我的。（注册在 main.py）"""
        blocked, gate_msg = await self._gate(event, "wish")
        if blocked:
            if gate_msg:
                yield event.plain_result(gate_msg)
            return

        async def call(group_id, player):
            return await self.wish_service.mine(group_id, player.player_id)

        async for result in self._wish_after_gate(event, "mine", call):
            yield result

    async def _wish_random_impl(self, event: "AstrMessageEvent"):
        """祈愿随机。（注册在 main.py）"""
        blocked, gate_msg = await self._gate(event, "wish")
        if blocked:
            if gate_msg:
                yield event.plain_result(gate_msg)
            return

        async def call(group_id, player):
            return await self.wish_service.random_self(
                group_id, player.player_id, player.player_name
            )

        async for result in self._wish_after_gate(event, "random", call):
            yield result

    # ── 诸神侧 ──

    async def _wish_admin_impl(self, event: "AstrMessageEvent"):
        """祈愿管理 <操作> [参数]。（注册在 main.py）

        走 `_gate(event, None)`（只过群访问控制）而不是 "wish"：功能关掉之后诸神
        仍要能进来收拾残局（清空遗留队伍），所以不套 feature 开关与状态阻断。
        也不查 wish_groups——群从名单里移除后，遗留队伍同样需要有人清理。

        权限是**诸神级**（超管 ∪ 白名单诸神 ∪ 本群群主/群管），不是「天梯榜管理」
        那套只认超管的 `_is_super_admin`——祈愿试炼的治理按设计交给诸神。
        """
        blocked, gate_msg = await self._gate(event, None)
        if blocked:
            if gate_msg:
                yield event.plain_result(gate_msg)
            return
        group_id = self._get_group_id(event)

        if not await self._check_perm(event):
            yield event.plain_result(PERMISSION_DENIED["god_only"])
            return

        args = self._get_args(event, "祈愿管理").strip()
        if not args:
            yield event.plain_result(WISH_ADMIN_USAGE)
            return

        action, _, rest = args.partition(" ")
        action, rest = action.strip(), rest.strip()
        service = self.wish_service

        if action in ("列表", "list"):
            outcome = await service.admin_list(group_id)
        elif action in ("已解散", "disbanded"):
            outcome = await service.admin_list_disbanded(group_id)
        elif action in ("名单", "roster"):
            team_name, numbers = split_tail_int(rest, count=2)
            if not team_name:
                yield event.plain_result("用法：祈愿管理 名单 <队名> [登神分] [觐见分]")
                return
            ladder = numbers[0] if len(numbers) > 0 else 0
            pilgrimage = numbers[1] if len(numbers) > 1 else 0
            outcome = await service.admin_roster(group_id, team_name, ladder, pilgrimage)
        elif action in ("换人", "swap"):
            who = rest.split()
            if len(who) != 2:
                yield event.plain_result(
                    "用法：祈愿管理 换人 <玩家1> <玩家2>\n"
                    "（玩家1 决定是哪支队伍：把他在队伍里的「房间状态」转给玩家2）"
                )
                return
            outcome = await service.admin_swap_players(group_id, who[0], who[1])
        elif action in ("解散", "disband"):
            if not rest:
                yield event.plain_result("用法：祈愿管理 解散 <队名>")
                return
            outcome = await service.admin_disband(group_id, rest)
        elif action in ("重命名", "rename"):
            pair = split_rename_pair(rest)
            if pair is None:
                yield event.plain_result(
                    "用法：祈愿管理 重命名 <旧名> <新名>\n"
                    "（两段都含空格时用 → 分隔，如：旧 名 → 新 名）"
                )
                return
            outcome = await service.admin_rename(group_id, pair[0], pair[1])
        elif action in ("移出", "remove"):
            tokens = rest.split()
            if len(tokens) < 2:
                yield event.plain_result("用法：祈愿管理 移出 <队名> <玩家名>")
                return
            outcome = await service.admin_remove_member(
                group_id, " ".join(tokens[:-1]), tokens[-1]
            )
        elif action in ("补位", "fill"):
            tokens = rest.split()
            if len(tokens) < 2:
                yield event.plain_result("用法：祈愿管理 补位 <队名> <玩家名>")
                return
            outcome = await service.admin_fill_member(
                group_id, " ".join(tokens[:-1]), tokens[-1]
            )
        elif action in ("延期", "extend"):
            team_name, numbers = split_tail_int(rest, count=1)
            if not team_name or len(numbers) != 1:
                yield event.plain_result("用法：祈愿管理 延期 <队名> <天数>")
                return
            outcome = await service.admin_extend(group_id, team_name, numbers[0])
        elif action in ("统计", "stats"):
            days = 7
            if rest:
                try:
                    days = int(rest.split()[0])
                except ValueError:
                    yield event.plain_result("用法：祈愿管理 统计 [天数]（默认 7 天）")
                    return
            outcome = await service.admin_stats(group_id, days)
        elif action in ("清空", "clear"):
            if rest != CONFIRM_TOKEN:
                yield event.plain_result(
                    "此操作会删除本群所有队伍与成员记录，并释放当天名额（不可撤销）。\n"
                    "已经发出去的状态不受影响。\n"
                    f"确认请发送：祈愿管理 清空 {CONFIRM_TOKEN}"
                )
                return
            outcome = await service.admin_clear(group_id)
        else:
            yield event.plain_result(f"未识别的操作：{action}\n\n{WISH_ADMIN_USAGE}")
            return

        await self._wish_broadcast(group_id, outcome.broadcasts)
        yield event.plain_result(outcome.reply)

    # ── 调度与成员变动（由 main.py 注入调度器 / 事件总线）──

    async def _wish_tick(self) -> None:
        """调度器每 30 秒调用一次：缺人提醒 + 超时解散。"""
        service = getattr(self, "wish_service", None)
        if service is None:
            return
        for group_id, text in await service.tick():
            await self._wish_broadcast(group_id, [text])
