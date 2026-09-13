"""
commands/ 包的约定守卫测试。

背景：本插件曾把 @filter.command 装饰器放进 mixin，结果出现「部分指令重复注册、
另一些完全不触发」——AstrBot 沿 MRO 扫描带装饰器的方法时会重复注册，注册异常
又中断了后续指令的注册。这些测试把「装饰器只留在 main.py」这条约定锁住。
"""

import ast
import inspect
from pathlib import Path

COMMANDS_DIR = Path(__file__).resolve().parent.parent / "commands"


def _command_modules():
    return sorted(p for p in COMMANDS_DIR.glob("*.py"))


def test_commands_package_is_importable_without_astrbot():
    """commands 包不得在运行时依赖 astrbot，否则指令层无法被测试。"""
    import astrbot_plugin_faith_ladder.commands as pkg

    assert "QueryCommandsMixin" in pkg.__all__
    assert "SharedSendMixin" in pkg.__all__


def test_no_filter_decorators_in_mixins():
    """mixin 里不得出现 @filter.* 装饰器——这是重复注册与指令不触发的根因。"""
    offenders = []
    for path in _command_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                src = ast.unparse(dec)
                if "filter." in src:
                    offenders.append(f"{path.name}:{node.lineno} {node.name} -> @{src}")
    assert not offenders, "mixin 中不允许出现 filter 装饰器:\n" + "\n".join(offenders)


def test_mixin_methods_are_private():
    """mixin 方法一律以下划线开头，避免与 main.py 的同名方法静默覆盖。"""
    from astrbot_plugin_faith_ladder.commands.query import QueryCommandsMixin
    from astrbot_plugin_faith_ladder.commands.shared import SharedSendMixin

    for mixin in (QueryCommandsMixin, SharedSendMixin):
        public = [
            name for name, _ in inspect.getmembers(mixin, inspect.isfunction)
            if not name.startswith("_")
        ]
        assert not public, f"{mixin.__name__} 存在公开方法（应加下划线前缀）: {public}"


def test_expected_implementations_exist():
    """试点迁移的两个实现体必须在位（装饰器仍在 main.py）。"""
    from astrbot_plugin_faith_ladder.commands.query import QueryCommandsMixin

    assert hasattr(QueryCommandsMixin, "_query_impl")
    assert hasattr(QueryCommandsMixin, "_query_inventory_impl")
