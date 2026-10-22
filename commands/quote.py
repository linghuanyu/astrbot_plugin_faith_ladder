"""
引用回复（mixin）。

AstrBot v4 的回复是一条 `MessageEventResult`，它的 `.chain` 就是消息段列表。
引用触发指令的那条消息，只需在 chain **头部**插一个 `Reply` 段——aiocqhttp
适配器对非特殊组件统一走 `toDict()`，`Reply.toDict()` 产出 OneBot V11 的
`{"type": "reply", "data": {"id": ...}}`，协议端据此挂上引用。

为什么收在 main.py 的指令包装器里：那里是全部指令回复唯一的收口点
（`async for result in self._xxx_impl(event): yield result`），包一次就能覆盖
成功/失败/用法/冷却所有分支，而实现体里的 294 处 `plain_result` 一个都不用动。

**fail-open**：引用是锦上添花，任何一环拿不到（开关关、取不到 message_id、
组件导入失败、结果结构不认识）都原样返回原结果。宁可少一个引用，也不能让
回复本身丢失或被替换掉。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

try:
    from astrbot.api import logger
except ImportError:  # 无 AstrBot 环境（如跑测试）时退回标准库日志
    import logging
    logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent


def _load_reply_component():
    """按框架版本取 `Reply` 组件；取不到返回 None（调用方据此跳过引用）。"""
    for import_path in (
        ("astrbot.core.message.components", "Reply"),
        ("astrbot.api.message_components", "Reply"),
    ):
        try:
            module = __import__(import_path[0], fromlist=[import_path[1]])
            component = getattr(module, import_path[1])
        except Exception as e:
            logger.debug(f"[QuoteReply] 导入 {import_path[0]}.{import_path[1]} 失败: {e}")
            continue
        if component is not None:
            return component
    return None


class QuoteReplyMixin:
    """给指令回复挂上「引用原消息」。"""

    def _quote_enabled(self) -> bool:
        """引用回复总开关（`quote_reply_enabled`，默认开启）。"""
        return bool(self._cfg("quote_reply_enabled"))

    def _quote_message_id(self, event: "AstrMessageEvent") -> str:
        """取触发消息的 id；取不到返回空串（调用方据此跳过引用）。"""
        message_obj = getattr(event, "message_obj", None)
        message_id = getattr(message_obj, "message_id", None)
        if message_id is None:
            return ""
        return str(message_id).strip()

    def _apply_quote(self, event: "AstrMessageEvent", result: Any) -> Any:
        """把 `result` 的 chain 头部插一段 `Reply`，返回同一个结果对象。

        不满足条件时**原样返回** `result`（见模块 docstring 的 fail-open 说明）。
        """
        try:
            if not self._quote_enabled():
                return result
            message_id = self._quote_message_id(event)
            if not message_id:
                return result

            chain = getattr(result, "chain", None)
            # 只处理消息段列表：None（纯事件结果）或非列表结构一律不动
            if not isinstance(chain, list):
                return result

            reply_cls = _load_reply_component()
            if reply_cls is None:
                return result

            reply_name = getattr(reply_cls, "__name__", "Reply")
            if any(type(seg).__name__ == reply_name for seg in chain):
                return result  # 已经带引用（例如实现体自己拼的），不重复插

            chain.insert(0, reply_cls(id=message_id))
            return result
        except Exception as e:
            logger.warning(f"[QuoteReply] 挂引用失败，按普通回复发出: {e}")
            return result
