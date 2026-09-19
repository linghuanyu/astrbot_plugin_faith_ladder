"""
通用发送辅助。框架相关调用集中在此，便于三处合并转发逻辑共用一份实现。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable, List

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent


def wrap_message_chain(components: Iterable):
    """把组件列表包成框架认识的 MessageChain。

    踩过的坑：`context.send_message(umo, [Plain(...)])` 在 AstrBot v4 上会抛
    `'list' object has no attribute 'chain'`（框架内部要访问 `.chain`），消息被
    静默丢弃——赌局的开奖播报与赠送超时通知都栽在这里。v4 要用
    `MessageChain(chain=[...])`；旧版本直接收 list，所以导入失败时原样返回。
    """
    items: List = list(components)
    for import_path in (
        ("astrbot.core.message.message_event_result", "MessageChain"),
        ("astrbot.api.message_components", "MessageChain"),
    ):
        try:
            module = __import__(import_path[0], fromlist=[import_path[1]])
            factory = getattr(module, import_path[1])
        except Exception:
            continue
        try:
            return factory(chain=items)
        except TypeError:
            try:
                return factory(items)
            except Exception:
                continue
    return items


class SharedSendMixin:
    """合并转发发送辅助。"""

    async def _send_forward_text(
        self, event: "AstrMessageEvent", group_id: str, nickname: str, text: str
    ) -> bool:
        """以合并转发的形式发送长文本。

        返回 True 表示已发送成功（并已 stop_event），调用方应直接 return；
        返回 False 表示合并转发不可用，调用方应回退为普通文本回复。
        """
        try:
            bot_id = int(event.get_self_id())
        except Exception:
            bot_id = 123456789
        from astrbot.core.message.components import Node, Plain
        nodes = [Node(
            user_id=bot_id,
            nickname=nickname,
            content=[Plain(text=text)]
        )]
        try:
            await event.bot.call_action(
                "send_group_forward_msg",
                group_id=int(group_id),
                messages=nodes
            )
        except Exception:
            return False
        event.stop_event()
        return True
