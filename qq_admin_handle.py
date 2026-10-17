"""
QQ 群管理命令逻辑。
照搬 astrbot_plugin_qqadmin 的实现，仅添加白名单权限控制。
成功操作静默（不发消息），错误情况保留提示。
需要 aiocqhttp (OneBot11) 协议支持。
"""

import asyncio
import inspect
import time
from typing import Optional, Tuple
from astrbot.core.message.components import At, Reply, Plain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot_plugin_faith_ladder.message_formatter import format_mute_list
from astrbot_plugin_faith_ladder.messages import PERMISSION_DENIED
from astrbot_plugin_faith_ladder.faith_messages import FAITH_MESSAGES, GENERIC_GOD_MESSAGES
from astrbot_plugin_faith_ladder.text_utils import render_template

try:
    from astrbot.api import logger
except ImportError:  # 无 AstrBot 环境（如跑测试）时退回标准库日志
    import logging
    logger = logging.getLogger(__name__)


async def _resolve(value):
    """兼容同步/异步回调：可 await 就 await，否则原样返回。

    三个回调（白名单、管理员、信仰）由插件注入，其中管理员判定是**同步**方法。
    直接 `await` 它的返回值会抛
    `TypeError: object bool can't be used in 'await' expression`，
    于是"只在 admin_ids、不在白名单"的管理员一用群管指令就报错。
    """
    if inspect.isawaitable(value):
        return await value
    return value


def get_ats(event: AiocqhttpMessageEvent) -> list:
    """获取被 at 的用户 ID 列表（排除机器人自身）"""
    return [
        str(seg.qq)
        for seg in event.get_messages()
        if isinstance(seg, At) and str(seg.qq) != event.get_self_id()
    ]


# === 禁言列表的取数与解析（纯函数，可直接单测） ===

# 「禁言到什么时候」的字段名。OneBot11 标准只规定了群成员对象里的
# shut_up_timestamp；get_group_shut_list 是 NapCat/LLOneBot 一类的扩展接口，
# 各家字段名不统一，能认的都认一遍，认不出就整份回退（见 normalize_shut_list）。
_SHUT_UNTIL_KEYS = ("shut_up_timestamp", "shut_up_time", "shutUpTime", "shutUpTimestamp")
_SHUT_USER_KEYS = ("user_id", "uin", "qq")

# 补昵称的调用上限：每条一次 API 调用，被禁言的人多起来不该把指令拖住
MAX_NAME_LOOKUPS = 20


def _shut_until(item: dict) -> Optional[int]:
    """取「解除禁言时间」并统一成秒；字段缺失或不可解析返回 None。

    有的实现给的是毫秒（13 位），不折算的话会显示成"剩余 5 万年"。
    """
    for key in _SHUT_UNTIL_KEYS:
        if key in item:
            try:
                value = int(item[key])
            except (TypeError, ValueError):
                return None
            return value // 1000 if value > 10**11 else value
    return None


def _shut_user(item: dict) -> str:
    """取被禁言者的 QQ 号，取不到返回空串。"""
    for key in _SHUT_USER_KEYS:
        value = item.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _mute_entry(item: dict, until: int) -> dict:
    """统一成 formatter 认的形状（昵称可能为空，展示时退回 QQ 号）。"""
    return {
        "user_id": _shut_user(item),
        "nickname": str(item.get("card") or item.get("nickname") or ""),
        "until": until,
    }


def muted_from_members(members, now: Optional[int] = None) -> Optional[list]:
    """从群成员表里挑出当前被禁言的人。

    **整份数据里一个禁言时间字段都没有时返回 None** —— 那说明协议端压根没提供
    （例如机器人没有群管理权限），此时"筛出来是空的"并不等于"没人被禁言"。
    调用方要据此回退或如实报错，不能静默回一句"现在没有人被禁言"。
    """
    now = int(time.time()) if now is None else int(now)
    rows = [m for m in members or [] if isinstance(m, dict)]
    if not any(key in row for row in rows for key in _SHUT_UNTIL_KEYS):
        return None
    out = []
    for row in rows:
        until = _shut_until(row)
        if until is None or until <= now or not _shut_user(row):
            continue
        out.append(_mute_entry(row, until))
    return out


def normalize_shut_list(raw, now: Optional[int] = None) -> Optional[list]:
    """整理协议端 get_group_shut_list 的返回值。

    认不出来（不是列表、条目里没有 QQ 号或没有禁言时间）返回 None，调用方回退到
    群成员表；**空列表是有效结果**（"确实没人被禁言"），两者必须区分开。
    有的实现把列表包在一层 dict 里，也拆开。已到期的条目直接丢掉——有些实现会把
    全群成员连 shut_up_timestamp=0 一起返回，不能照单全收。
    """
    if isinstance(raw, dict):
        raw = next((v for v in raw.values() if isinstance(v, list)), None)
    if not isinstance(raw, list):
        return None
    now = int(time.time()) if now is None else int(now)
    out = []
    for item in raw:
        if not isinstance(item, dict):
            return None
        until = _shut_until(item)
        if until is None or not _shut_user(item):
            return None
        if until > now:
            out.append(_mute_entry(item, until))
    return out


class QQAdminHandler:
    """QQ 群管命令实现。成功显示信仰主题消息，错误保留提示。"""

    def __init__(
        self,
        check_perm_fn,  # async (user_id: str) -> bool
        check_admin_fn,  # (event) -> bool
        get_faith_fn=None,  # async (user_id: str) -> str|None
        gate_fn=None,  # async (event, action: str) -> (blocked: bool, message: str|None)
    ):
        """注入权限/信仰/管理员判定回调，使群管逻辑不直接依赖插件实例，便于单独复用与测试。"""
        self._check_perm = check_perm_fn
        self._is_admin = check_admin_fn
        if get_faith_fn is None:
            async def _no_faith(uid):
                return None
            get_faith_fn = _no_faith
        self._get_faith = get_faith_fn
        self._gate_fn = gate_fn

    async def _preflight(self, event) -> Tuple[bool, Optional[str]]:
        """入口闸门：群访问控制与功能开关。未注入回调时不拦截（保持可独立测试）。"""
        if self._gate_fn is None:
            return False, None
        return await _resolve(self._gate_fn(event, "qq_admin"))

    async def _get_faith_message(self, event: AiocqhttpMessageEvent, action: str, **kwargs) -> str:
        """获取信仰专属消息。"""
        import random

        user_id = str(event.get_sender_id())
        faith = await _resolve(self._get_faith(user_id))

        messages = FAITH_MESSAGES.get(faith, {}).get(action, [])
        if messages:
            msg = random.choice(messages)
            return render_template(msg, kwargs)
        # 回退到通用消息
        generic = GENERIC_GOD_MESSAGES.get(action, [])
        if generic:
            return render_template(random.choice(generic), kwargs)
        return ""

    async def _check_permission(self, event: AiocqhttpMessageEvent) -> bool:
        """检查用户是否有群管权限（复用白名单系统）"""
        user_id = str(event.get_sender_id())
        has_perm = await _resolve(self._check_perm(user_id))
        if has_perm:
            return True
        return bool(await _resolve(self._is_admin(event)))

    async def _get_member_info(self, event: AiocqhttpMessageEvent, user_id: str) -> dict:
        """获取群成员信息（一次 API 调用获取角色、名片、昵称）。"""
        try:
            return await event.bot.get_group_member_info(
                group_id=int(event.get_group_id()), user_id=int(user_id)
            ) or {}
        except Exception:
            return {}

    # === 禁言 ===

    async def handle_ban(self, event: AiocqhttpMessageEvent):
        """禁言 <秒数> @用户 — 成功静默，错误保留"""
        blocked, block_msg = await self._preflight(event)
        if blocked:
            if block_msg:
                yield event.plain_result(block_msg)
                event.stop_event()
            return
        if not await self._check_permission(event):
            yield event.plain_result(PERMISSION_DENIED["god_only"])
            event.stop_event()
            return

        targets = get_ats(event)
        if not targets:
            yield event.plain_result("请 @ 要禁言的用户。")
            event.stop_event()
            return

        text = event.message_str.strip()
        duration = 60
        # 取**第一个**纯数字词作为时长，且只看 Plain 段（避免 @ 段里的 qq 号被误识别）。
        # 此前内层 break 只跳出内层循环，后面出现的数字会覆盖前面的，
        # 例如「禁言 60 @某人 我记得 30 秒」会被当成 30 秒。
        for seg in event.get_messages():
            if not (isinstance(seg, Plain) and seg.text):
                continue
            first_number = next((p for p in seg.text.split() if p.isdigit()), None)
            if first_number is not None:
                duration = int(first_number)
                break

        errors = []
        success_targets = []
        for uid in targets:
            info = await self._get_member_info(event, uid)
            nickname = info.get("card") or info.get("nickname") or str(uid)
            if not info:
                # 角色未知时不冒险操作，避免 API 抖动导致群主/管理员被误禁言
                errors.append(f"{nickname} — 无法获取群成员信息（角色未知），已跳过")
                continue
            role = info.get("role", "member")

            if role in ("owner", "admin"):
                label = "群主" if role == "owner" else "管理员"
                errors.append(f"{nickname} — {label}不可操作")
                continue
            try:
                await event.bot.set_group_ban(
                    group_id=int(event.get_group_id()),
                    user_id=int(uid),
                    duration=duration,
                )
                success_targets.append(nickname)
            except Exception as e:
                errors.append(f"禁言失败：{e}")

        # 错误与成功都要报：只报错误会让人以为成功的那些也没执行
        if errors:
            yield event.plain_result("\n".join(errors))
        if success_targets:
            # 成功：显示信仰主题消息
            for name in success_targets:
                msg = await self._get_faith_message(event, "ban_success", target_name=name)
                if msg:
                    yield event.plain_result(msg)
                    break  # 只发一条
        event.stop_event()

    # === 解禁 ===

    async def handle_unban(self, event: AiocqhttpMessageEvent):
        """解禁 @用户 — 成功显示信仰消息"""
        blocked, block_msg = await self._preflight(event)
        if blocked:
            if block_msg:
                yield event.plain_result(block_msg)
                event.stop_event()
            return
        if not await self._check_permission(event):
            yield event.plain_result(PERMISSION_DENIED["god_only"])
            event.stop_event()
            return

        targets = get_ats(event)
        if not targets:
            yield event.plain_result("请 @ 要解禁的用户。")
            event.stop_event()
            return

        errors = []
        success_targets = []
        for uid in targets:
            info = await self._get_member_info(event, uid)
            nickname = info.get("card") or info.get("nickname") or str(uid)
            try:
                await event.bot.set_group_ban(
                    group_id=int(event.get_group_id()),
                    user_id=int(uid),
                    duration=0,
                )
                success_targets.append(nickname)
            except Exception as e:
                # 此前这里是 except: pass —— 机器人没有群管权限时用户得不到任何反馈，
                # 无法区分「已解禁但静默」和「根本没执行」
                errors.append(f"解禁失败：{e}")

        if errors:
            yield event.plain_result("\n".join(errors))
        if success_targets:
            for name in success_targets:
                msg = await self._get_faith_message(event, "unban_success", target_name=name)
                if msg:
                    yield event.plain_result(msg)
                    break
        event.stop_event()

    # === 禁言列表 ===

    async def handle_mute_list(self, event: AiocqhttpMessageEvent):
        """禁言列表 — 列出本群当前被禁言的人与剩余时间"""
        blocked, block_msg = await self._preflight(event)
        if blocked:
            if block_msg:
                yield event.plain_result(block_msg)
                event.stop_event()
            return
        if not await self._check_permission(event):
            yield event.plain_result(PERMISSION_DENIED["god_only"])
            event.stop_event()
            return

        entries, error = await self._fetch_muted(event, int(event.get_group_id()))
        if error:
            yield event.plain_result(error)
            event.stop_event()
            return
        await self._fill_missing_names(event, entries)
        yield event.plain_result(format_mute_list(entries))
        event.stop_event()

    async def _fetch_muted(self, event, group_id: int):
        """取本群当前被禁言的成员。

        返回 (entries, error)：entries 为空列表代表"确实没人被禁言"，取数失败
        返回 (None, 说明)——两者不能混为一谈，静默说一句"没人被禁言"会让诸神
        以为禁言根本没生效。

        先走群成员表：它是 OneBot11 标准接口，还自带群名片，一次调用就够。
        它取不到、整份数据里没有禁言时间字段、或者结果是空的（"没人被禁言"）
        时，再问一次协议端的扩展接口 get_group_shut_list —— 有的协议端在机器人
        不是管理员时会把 shut_up_timestamp 一律给 0，只信成员表这一份，就会把
        "读不到"显示成"没人被禁言"。
        """
        now = int(time.time())
        members_error = None
        try:
            members = await event.bot.get_group_member_list(group_id=group_id)
        except Exception as e:
            members = None
            members_error = e
        entries = muted_from_members(members, now) if members is not None else None

        shut_entries = None
        if not entries:
            shut_entries = await self._probe_shut_list(event, group_id, now)
            if shut_entries:
                return shut_entries, None

        if entries is not None:
            return entries, None
        if shut_entries is not None:
            return shut_entries, None
        if members_error is not None:
            return None, f"取不到群成员列表，禁言列表查询失败：{members_error}"
        return None, "取不到禁言信息：群成员表里没有禁言时间字段，协议端也不支持禁言列表接口。请确认机器人是群管理员。"

    async def _probe_shut_list(self, event, group_id: int, now: int) -> Optional[list]:
        """问一次扩展接口 get_group_shut_list。

        返回列表（可能为空）表示协议端认这个接口；返回 None 表示没有这个接口或
        返回值认不出来——两种情况对调用方的含义完全不同，不能合并。
        """
        try:
            raw = await event.bot.call_action("get_group_shut_list", group_id=group_id)
        except Exception as e:
            logger.debug(f"协议端没有 get_group_shut_list（{e}），禁言列表改用群成员表")
            return None
        return normalize_shut_list(raw, now)


    async def _fill_missing_names(self, event, entries) -> None:
        """给没有昵称的条目补上群名片/昵称（扩展接口的返回常常不带昵称）。

        只补前 MAX_NAME_LOOKUPS 条：每条一次 API 调用，不能让人多起来就拖住指令；
        补不到就只显示 QQ 号。
        """
        for entry in entries[:MAX_NAME_LOOKUPS]:
            if entry.get("nickname"):
                continue
            info = await self._get_member_info(event, entry["user_id"])
            if info:
                entry["nickname"] = info.get("card") or info.get("nickname") or ""

    # === 踢人 ===

    async def handle_kick(self, event: AiocqhttpMessageEvent):
        """踢出 @用户 — 成功显示信仰消息，错误保留"""
        blocked, block_msg = await self._preflight(event)
        if blocked:
            if block_msg:
                yield event.plain_result(block_msg)
                event.stop_event()
            return
        if not await self._check_permission(event):
            yield event.plain_result(PERMISSION_DENIED["god_only"])
            event.stop_event()
            return

        targets = get_ats(event)
        if not targets:
            yield event.plain_result("请 @ 要踢出的用户。")
            event.stop_event()
            return

        errors = []
        success_targets = []
        for uid in targets:
            info = await self._get_member_info(event, uid)
            nickname = info.get("card") or info.get("nickname") or str(uid)
            if not info:
                # 角色未知时不冒险操作，避免 API 抖动导致群主/管理员被误踢
                errors.append(f"{nickname} — 无法获取群成员信息（角色未知），已跳过")
                continue
            role = info.get("role", "member")

            if role in ("owner", "admin"):
                label = "群主" if role == "owner" else "管理员"
                errors.append(f"{nickname} — {label}不可操作")
                continue
            try:
                await event.bot.set_group_kick(
                    group_id=int(event.get_group_id()),
                    user_id=int(uid),
                    reject_add_request=False,
                )
                success_targets.append(nickname)
            except Exception as e:
                errors.append(f"踢出失败：{e}")

        # 同上：部分成功时成功项也要报出来
        if errors:
            yield event.plain_result("\n".join(errors))
        if success_targets:
            for name in success_targets:
                msg = await self._get_faith_message(event, "kick_success", target_name=name)
                if msg:
                    yield event.plain_result(msg)
                    break
        event.stop_event()

    # === 撤回 ===

    async def handle_recall(self, event: AiocqhttpMessageEvent):
        """撤回消息 — 成功静默，错误保留"""
        blocked, block_msg = await self._preflight(event)
        if blocked:
            if block_msg:
                yield event.plain_result(block_msg)
                event.stop_event()
            return
        if not await self._check_permission(event):
            yield event.plain_result(PERMISSION_DENIED["god_only"])
            event.stop_event()
            return

        client = event.bot
        chain = event.get_messages()
        # 空消息链会让 chain[0] 抛 IndexError 且不执行 stop_event（相邻的精华处理有守卫）
        first_seg = chain[0] if chain else None
        if first_seg is None:
            yield event.plain_result("没有可撤回的消息。")
            event.stop_event()
            return

        # 方式1: 撤回引用的消息
        if isinstance(first_seg, Reply):
            try:
                await client.delete_msg(message_id=int(first_seg.id))
                # 成功：静默
            except Exception:
                yield event.plain_result("消息已过期或不存在")
            event.stop_event()
            return

        # 方式2: 撤回 @ 用户的最近消息
        if any(isinstance(seg, At) for seg in chain):
            target_ids = get_ats(event) or [event.get_self_id()]
            target_ids = {str(uid) for uid in target_ids}

            text = event.message_str.strip()
            count = 10
            # 只从 Plain 文本段提取数量
            for seg in event.get_messages():
                if isinstance(seg, Plain) and seg.text:
                    for part in seg.text.split():
                        if part.isdigit():
                            count = min(int(part), 50)
                            break

            try:
                result = await client.api.call_action(
                    "get_group_msg_history",
                    group_id=int(event.get_group_id()),
                    message_seq=0,
                    count=count,
                    reverseOrder=True,
                )
                messages = list(reversed(result.get("messages", [])))
            except Exception:
                messages = []

            sem = asyncio.Semaphore(10)

            async def try_delete(message):
                """删除单条消息，失败静默（消息可能已被撤回或权限不足）。"""
                # 历史消息可能缺 sender（系统/通知类），用 .get 链避免 KeyError
                # 把整个 asyncio.gather 带崩
                sender_id = str((message.get("sender") or {}).get("user_id", ""))
                if sender_id not in target_ids:
                    return
                async with sem:
                    try:
                        await client.delete_msg(message_id=message["message_id"])
                    except Exception:
                        pass

            tasks = [try_delete(msg) for msg in messages]
            await asyncio.gather(*tasks)

            # 成功：静默
            event.stop_event()
            return

        yield event.plain_result("请引用消息或 @ 用户。")
        event.stop_event()

    # === 全员禁 ===

    async def handle_mute_all(self, event: AiocqhttpMessageEvent):
        """全员禁言 — 成功静默"""
        blocked, block_msg = await self._preflight(event)
        if blocked:
            if block_msg:
                yield event.plain_result(block_msg)
                event.stop_event()
            return
        if not await self._check_permission(event):
            yield event.plain_result(PERMISSION_DENIED["god_only"])
            event.stop_event()
            return
        try:
            await event.bot.set_group_whole_ban(
                group_id=int(event.get_group_id()), enable=True
            )
            # 成功：静默
        except Exception as e:
            yield event.plain_result(f"操作失败: {e}")
        event.stop_event()

    # === 全员解 ===

    async def handle_unmute_all(self, event: AiocqhttpMessageEvent):
        """关闭全员禁言 — 成功静默"""
        blocked, block_msg = await self._preflight(event)
        if blocked:
            if block_msg:
                yield event.plain_result(block_msg)
                event.stop_event()
            return
        if not await self._check_permission(event):
            yield event.plain_result(PERMISSION_DENIED["god_only"])
            event.stop_event()
            return
        try:
            await event.bot.set_group_whole_ban(
                group_id=int(event.get_group_id()), enable=False
            )
            # 成功：静默
        except Exception as e:
            yield event.plain_result(f"操作失败: {e}")
        event.stop_event()

    # === 设置精华 ===

    async def handle_set_essence(self, event: AiocqhttpMessageEvent):
        """设置精华消息 — 引用一条消息设置为精华，成功静默"""
        blocked, block_msg = await self._preflight(event)
        if blocked:
            if block_msg:
                yield event.plain_result(block_msg)
                event.stop_event()
            return
        if not await self._check_permission(event):
            yield event.plain_result(PERMISSION_DENIED["god_only"])
            event.stop_event()
            return

        chain = event.get_messages()
        first_seg = chain[0] if chain else None

        if not isinstance(first_seg, Reply):
            yield event.plain_result("请引用要设置为精华的消息。")
            event.stop_event()
            return

        try:
            await event.bot.set_essence_msg(message_id=int(first_seg.id))
            # 成功：静默
        except Exception as e:
            yield event.plain_result(f"设置精华失败: {e}")
        event.stop_event()

    # === 移除精华 ===

    async def handle_remove_essence(self, event: AiocqhttpMessageEvent):
        """移除精华消息 — 引用一条消息移除精华，成功静默"""
        blocked, block_msg = await self._preflight(event)
        if blocked:
            if block_msg:
                yield event.plain_result(block_msg)
                event.stop_event()
            return
        if not await self._check_permission(event):
            yield event.plain_result(PERMISSION_DENIED["god_only"])
            event.stop_event()
            return

        chain = event.get_messages()
        first_seg = chain[0] if chain else None

        if not isinstance(first_seg, Reply):
            yield event.plain_result("请引用要移除精华的消息。")
            event.stop_event()
            return

        try:
            await event.bot.delete_essence_msg(message_id=int(first_seg.id))
            # 成功：静默
        except Exception as e:
            yield event.plain_result(f"移除精华失败: {e}")
        event.stop_event()
