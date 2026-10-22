"""
引用回复：把指令回复挂到触发它的那条消息上。

收口点在 main.py 的指令包装器（`yield self._apply_quote(event, result)`），
所以这里既测 `_apply_quote` 的行为，也测"哪些指令接进了这套收口"。
真正的 `Reply` 组件来自 AstrBot，测试环境用桩替身（形状与 v4 一致：
`Reply(id=...)`，`toDict()` 产出 OneBot V11 的 reply 段）。
"""

import ast
import sys
import types
from pathlib import Path

import pytest

from astrbot_plugin_faith_ladder.commands.config import ConfigMixin
from astrbot_plugin_faith_ladder.commands.quote import QuoteReplyMixin

ROOT = Path(__file__).resolve().parents[1]

# 接进引用回复的指令（其余指令保持原样，避免把榜单/祈愿/群管也改掉）
QUOTED_COMMANDS = {
    "cmd_query",
    "cmd_register_player",
    "cmd_register_faith",
    "cmd_set_class",
    "cmd_take_oath",
    "cmd_add_score",
    "cmd_abandon_oath",
    "cmd_give_item",
    "cmd_remove_item",
    "cmd_gift_item",
    "cmd_accept_gift",
    "cmd_reject_gift",
    "cmd_admin",
    "cmd_help",
}


class _Reply:
    """与 AstrBot v4 的 Reply 同形：只用到 id，toDict 输出 OneBot reply 段。"""

    def __init__(self, id=None):
        self.id = id

    def toDict(self):
        return {"type": "reply", "data": {"id": str(self.id)}}


class _Plain:
    def __init__(self, text=""):
        self.text = text


class _At:
    def __init__(self, qq=None):
        self.qq = qq


@pytest.fixture
def reply_component(monkeypatch):
    """注入最小 astrbot 桩，让 `_load_reply_component()` 取到 _Reply。"""
    def _mod(name, **attrs):
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        monkeypatch.setitem(sys.modules, name, module)

    _mod("astrbot")
    _mod("astrbot.core")
    _mod("astrbot.core.message")
    _mod("astrbot.core.message.components", Reply=_Reply, Plain=_Plain, At=_At)
    return _Reply


@pytest.fixture
def no_reply_component(monkeypatch):
    """模拟组件导入失败：两个候选路径都不可用。"""
    for name in ("astrbot.core.message.components", "astrbot.api.message_components"):
        monkeypatch.setitem(sys.modules, name, None)


class _Result:
    """MessageEventResult 的最小替身：只需要 chain 能被读写。"""

    def __init__(self, chain=None):
        self.chain = chain


class _MessageObj:
    def __init__(self, message_id="12345"):
        self.message_id = message_id


class _Event:
    def __init__(self, message_id="12345"):
        if message_id is not _MISSING:
            self.message_obj = _MessageObj(message_id)


_MISSING = object()


class _Host(ConfigMixin, QuoteReplyMixin):
    def __init__(self, config=None):
        self.config = config or {}


@pytest.fixture
def reply_component_and_host(reply_component):
    return reply_component, _Host()


class TestApplyQuote:
    def test_enabled_by_default_and_inserts_reply(self, reply_component_and_host):
        reply_cls, host = reply_component_and_host
        result = _Result([_Plain("已录入")])

        returned = host._apply_quote(_Event("777"), result)

        assert returned is result, "必须返回同一个结果对象，调用方才能照旧 yield"
        assert isinstance(result.chain[0], reply_cls)
        assert result.chain[0].id == "777"
        assert result.chain[1].text == "已录入", "原内容不能被替换掉"

    def test_message_id_is_normalized_to_string(self, reply_component_and_host):
        reply_cls, host = reply_component_and_host
        result = _Result([_Plain("x")])

        host._apply_quote(_Event(12345), result)

        assert result.chain[0].id == "12345"
        assert isinstance(result.chain[0].id, str)

    def test_switch_off_leaves_result_untouched(self, reply_component):
        reply_cls = reply_component
        host = _Host({"quote_reply_enabled": False})
        plain = _Plain("已录入")
        result = _Result([plain])

        returned = host._apply_quote(_Event("777"), result)

        assert returned is result
        assert result.chain == [plain], "关掉开关后应与改造前的回复完全一致"

    def test_missing_message_obj_is_safe(self, reply_component):
        host = _Host()
        plain = _Plain("x")
        result = _Result([plain])

        assert host._apply_quote(_Event(_MISSING), result) is result
        assert result.chain == [plain]

    @pytest.mark.parametrize("message_id", [None, "", "   "])
    def test_empty_message_id_is_safe(self, reply_component, message_id):
        host = _Host()
        plain = _Plain("x")
        result = _Result([plain])

        host._apply_quote(_Event(message_id), result)

        assert result.chain == [plain]

    def test_component_import_failure_is_safe(self, no_reply_component):
        host = _Host()
        plain = _Plain("x")
        result = _Result([plain])

        # 组件取不到时不能抛，也不能改结果：宁可没有引用，回复不能丢
        assert host._apply_quote(_Event("777"), result) is result
        assert result.chain == [plain]

    def test_existing_reply_is_not_duplicated(self, reply_component):
        reply_cls = reply_component
        host = _Host()
        existing = reply_cls(id="old")
        result = _Result([existing, _Plain("x")])

        host._apply_quote(_Event("777"), result)

        assert result.chain == [existing, _Plain("x")] or (
            isinstance(result.chain[0], reply_cls) and result.chain[0].id == "old"
        )
        assert sum(isinstance(seg, reply_cls) for seg in result.chain) == 1

    def test_result_without_chain_is_safe(self, reply_component):
        host = _Host()
        result = _Result(None)

        assert host._apply_quote(_Event("777"), result) is result
        assert result.chain is None

    def test_chain_result_keeps_at_segment(self, reply_component):
        """录入玩家的成功回复是 [At, Plain]：引用插在最前，At 仍要留在原位。"""
        reply_cls = reply_component
        host = _Host()
        at = _At(qq=777)
        result = _Result([at, _Plain(" 已录入")])

        host._apply_quote(_Event("9"), result)

        assert isinstance(result.chain[0], reply_cls)
        assert result.chain[1] is at
        assert result.chain[2].text == " 已录入"


class TestQuotedCommandWiring:
    """静态守卫：只有指定的 14 条指令接进了引用回复。"""

    @staticmethod
    def _wrappers_using_quote() -> set:
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        return {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
            and "_apply_quote(" in ast.unparse(node)
        }

    def test_exact_command_set(self):
        assert self._wrappers_using_quote() == QUOTED_COMMANDS

    def test_commands_still_yield_original_results(self):
        """包装器仍是 `async for ... yield`，引用只是包一层，不吞结果。"""
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name in QUOTED_COMMANDS:
                body = ast.unparse(node)
                assert "async for result in" in body, f"{node.name} 不再是透传包装器"
                assert "yield self._apply_quote(event, result)" in body, f"{node.name} 漏了引用"
