"""
合并转发的 payload 回归测试。

背景：`_send_forward_text` 是裸 OneBot 调用，参数由 aiocqhttp 直接 `json.dumps`
（WS 路径走 `json.dumps`，HTTP 路径走 httpx 的 `json=`）。此前传的是 AstrBot 的
Node 组件（pydantic 模型），序列化时抛
`TypeError: Object of type Node is not JSON serializable`，又被收尾的
`except Exception` 吞掉 —— 天梯榜 / 觐见榜 / 储物空间三个功能一直静默回退成纯文本，
而且没有日志可查。这里把两件事各自钉住：「参数必须可 JSON 序列化」与「node 字段名」。

不用 astrbot 桩：修好之后 commands/shared.py 不碰任何框架组件。
"""

import ast
import json
from pathlib import Path

from astrbot_plugin_faith_ladder.commands.shared import SharedSendMixin

ROOT = Path(__file__).resolve().parents[1]

# 会被误当参数塞进 call_action 的消息组件（AstrBot 的，不是 OneBot 的）
COMPONENT_NAMES = {"Node", "Nodes", "Plain", "At", "Reply", "Image", "File"}


class _FakeBot:
    """假客户端：call_action 里真做一次 json.dumps —— aiocqhttp 就是这么发的。"""

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    async def call_action(self, action, **params):
        json.dumps(params)
        if self.fail:
            raise RuntimeError("ActionFailed<retcode=1200, message=权限不足>")
        self.calls.append((action, params))
        return {}


class _FakeEvent:
    def __init__(self, bot=None, self_id="1"):
        self.bot = bot or _FakeBot()
        self._self_id = self_id
        self.stopped = False

    def get_self_id(self):
        return self._self_id

    def stop_event(self):
        self.stopped = True


class _Host(SharedSendMixin):
    """借 mixin 的宿主类：这里只用到 _send_forward_text 一个方法。"""


class TestForwardPayload:
    async def test_payload_is_json_serializable_and_shaped(self):
        bot = _FakeBot()
        event = _FakeEvent(bot)

        ok = await _Host()._send_forward_text(event, "821721918", "天梯榜", "很长很长的榜单")

        assert ok is True
        assert event.stopped is True, "发送成功后要 stop_event，避免调用方再发一条纯文本"
        action, params = bot.calls[0]
        assert action == "send_group_forward_msg"
        assert params["group_id"] == 821721918, "群号要转成 int"
        node = params["messages"][0]
        assert node["type"] == "node"
        assert node["data"]["nickname"] == "天梯榜"
        assert node["data"]["content"] == [{"type": "text", "data": {"text": "很长很长的榜单"}}]

    async def test_node_author_fields_are_onebot_ones(self):
        """node 的作者字段是 user_id / nickname。

        以前写的是 AstrBot Node 的 `user_id=` / `nickname=` 关键字参数，那是照抄
        组件构造的写法；OneBot 这层的节点作者字段在 `data` 里，键名一样但必须显式
        落下。写错不会报错（pydantic 默认忽略多余 kwargs），只会在卡片上显示成空作者。
        """
        bot = _FakeBot()
        await _Host()._send_forward_text(_FakeEvent(bot, self_id="1423825616"), "1", "觐见榜", "x")

        data = bot.calls[0][1]["messages"][0]["data"]
        assert set(data) == {"user_id", "nickname", "content"}
        assert data["user_id"] == "1423825616", "节点作者是机器人自己，且必须是字符串"

    async def test_unparsable_self_id_still_serializes(self):
        """self_id 取不到时用占位号，不能因此把整条转发弄丢。"""
        bot = _FakeBot()
        ok = await _Host()._send_forward_text(_FakeEvent(bot, self_id=None), "1", "储物空间", "x")

        assert ok is True
        assert bot.calls[0][1]["messages"][0]["data"]["user_id"] == "123456789"

    async def test_failure_returns_false_and_leaves_a_trace(self, caplog):
        """发送失败要返回 False（调用方回退纯文本）**并留下日志**。

        此前这里静默 return False：三个功能一直退化，坏在哪没有任何线索。
        """
        bot = _FakeBot(fail=True)
        event = _FakeEvent(bot)

        with caplog.at_level("WARNING"):
            ok = await _Host()._send_forward_text(event, "1", "天梯榜", "x")

        assert ok is False
        assert event.stopped is False, "没发出去就不该抢事件"
        assert "合并转发发送失败" in caplog.text
        assert "1200" in caplog.text, "协议端给的 retcode 要带进日志"


def test_no_message_components_passed_to_call_action():
    """静态守卫：`call_action` 的参数里不许出现消息组件构造。

    这正是本次修的 bug：组件是 pydantic 模型，aiocqhttp 序列化不了，报错又被
    `except Exception` 吞掉。裸协议调用只收可 JSON 序列化的 dict。
    """
    def is_component(node) -> bool:
        if not isinstance(node, ast.Call):
            return False
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        return name in COMPONENT_NAMES

    def has_component_arg(call: ast.Call) -> bool:
        values = list(call.args) + [kw.value for kw in call.keywords]
        for value in values:
            if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
                if any(is_component(item) for item in value.elts):
                    return True
            elif is_component(value):
                return True
        return False

    offenders = []
    for path in [ROOT / "main.py"] + sorted(ROOT.glob("*.py")) + sorted((ROOT / "commands").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "call_action" and has_component_arg(node):
                offenders.append(f"{path.name}:{node.lineno}")

    assert offenders == [], "这些 call_action 传了消息组件（不可 JSON 序列化）：" + "；".join(offenders)
