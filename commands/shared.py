"""
通用发送辅助。框架相关调用集中在此，便于三处合并转发逻辑共用一份实现。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable, List

if TYPE_CHECKING:
    from astrbot.api.event import AstrMessageEvent

try:
    from astrbot.api import logger
except ImportError:  # 无 AstrBot 环境（如跑测试）时退回标准库日志
    import logging
    logger = logging.getLogger(__name__)


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

        这里是**裸 OneBot 调用**：AstrBot 没有「立即发送合并转发并告知成败」的
        高层 API，适配器只在消息链里遇到 Node/Nodes 组件时才自己转换。而
        `call_action` 的参数会被直接 `json.dumps`（WS 走 `json.dumps`，HTTP 走
        httpx 的 `json=`），所以必须给纯 dict。此前传的是 AstrBot 的 Node 组件
        （pydantic 模型），序列化时抛
        `TypeError: Object of type Node is not JSON serializable`，被收尾的
        `except Exception` 吞掉——天梯榜 / 觐见榜 / 储物空间因此一直静默回退成
        纯文本，坏在哪只有日志能回答。

        payload 形状与适配器 `Nodes.to_dict()` 的产物一致：节点作者字段是
        `user_id` / `nickname`（不是 AstrBot Node 的 `name` / `uin`），
        正文用 OneBot 的数组形态。正文只有纯文本，图片才需要先转 base64。
        """
        try:
            bot_id = int(event.get_self_id())
        except (TypeError, ValueError):
            bot_id = 123456789
        payload = {
            "messages": [
                {
                    "type": "node",
                    "data": {
                        "user_id": str(bot_id),
                        "nickname": nickname,
                        "content": [{"type": "text", "data": {"text": text}}],
                    },
                }
            ]
        }
        try:
            await event.bot.call_action(
                "send_group_forward_msg",
                group_id=int(group_id),
                **payload,
            )
        except Exception as e:
            logger.warning(f"合并转发发送失败，回退纯文本: {e}")
            return False
        event.stop_event()
        return True
