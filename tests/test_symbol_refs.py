"""
属性存在性静态检查：`self.<attr>` 与 `self.<service>.<member>` 是否真实存在。

补上这条的原因：未定义名扫描只看函数体内“光秃秃”的名字，看不见属性访问。
`initialize()` 里 `self.db_manager.purge_old_score_history` 指向一个已被删除的方法，
两条既有检查都没抓到，导致插件静默降级了数个版本。这里把服务对象的方法/属性表
与调用点对照一遍。

纯 AST 实现，不导入任何模块（生产模块依赖 astrbot，测试环境导入不了）。
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# 属性名 → (文件, 类名)。插件里对这些对象的方法调用会被逐一核对。
SERVICES = {
    "db_manager": ("db_manager.py", "DatabaseManager"),
    "ladder_service": ("ladder_service.py", "LadderService"),
    "permission_service": ("permission_service.py", "PermissionService"),
    "cooldown_manager": ("cooldown.py", "CooldownManager"),
    "_qq_admin": ("qq_admin_handle.py", "QQAdminHandler"),
    "_scheduler": ("scheduler_service.py", "SchedulerService"),
}

# 由框架注入、不在插件类里赋值的属性
FRAMEWORK_ATTRS = {
    "context",  # Star.__init__ 传入
}

PLUGIN_FILES = ["main.py"] + [f"commands/{p.name}" for p in sorted((ROOT / "commands").glob("*.py"))]


def class_symbols(source: str, cls_name: str) -> set:
    """收集类的方法名与实例属性名（self.X = ... / self.X: T = ...）。"""
    tree = ast.parse(source)
    cls = next((n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == cls_name), None)
    if cls is None:
        return set()
    names = set()
    for n in ast.walk(cls):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(n.name)
        elif isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store) \
                and isinstance(n.value, ast.Name) and n.value.id == "self":
            names.add(n.attr)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Attribute) \
                and isinstance(n.target.value, ast.Name) and n.target.value.id == "self":
            names.add(n.target.attr)
    return names


def load_service_symbols() -> dict:
    out = {}
    for attr, (fname, cls_name) in SERVICES.items():
        src = (ROOT / fname).read_text(encoding="utf-8")
        syms = class_symbols(src, cls_name)
        assert syms, f"{fname} 里找不到类 {cls_name}"
        out[attr] = syms
    return out


def collect_plugin_symbols(sources: dict) -> set:
    """插件类本身可用的符号 = main.py 的类 + 各 mixin 的方法与实例属性。"""
    syms = set()
    for src in sources.values():
        for cls in [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ClassDef)]:
            if cls.name == "FaithLadderPlugin" or cls.name.endswith(("CommandsMixin", "SendMixin")):
                syms |= class_symbols(src, cls.name)
    return syms


def find_violations(sources: dict, service_symbols: dict, plugin_symbols: set) -> list:
    violations = []
    for fname, src in sources.items():
        tree = ast.parse(src)
        for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            for node in ast.walk(fn):
                if not (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute)):
                    continue
                inner = node.value
                if not (isinstance(inner.value, ast.Name) and inner.value.id == "self"):
                    continue
                attr = inner.attr
                if attr in service_symbols:
                    if node.attr not in service_symbols[attr]:
                        violations.append(
                            f"{fname}:{node.lineno} self.{attr}.{node.attr} 不存在于 {SERVICES[attr][1]}"
                        )
                elif attr not in plugin_symbols and attr not in FRAMEWORK_ATTRS:
                    violations.append(
                        f"{fname}:{node.lineno} self.{attr} 未定义（却访问 .{node.attr}）"
                    )
    return violations


def test_no_missing_attribute_references():
    """插件里对服务对象与自身属性的引用都必须真实存在。"""
    sources = {f: (ROOT / f).read_text(encoding="utf-8") for f in PLUGIN_FILES}
    violations = find_violations(sources, load_service_symbols(), collect_plugin_symbols(sources))
    assert not violations, "发现不存在的属性引用：\n" + "\n".join(violations)


def test_checker_detects_missing_service_method():
    """自检：检查器确实能报出问题（否则上面的"零发现"没有意义）。"""
    service_symbols = load_service_symbols()
    fake = {
        "fake.py": (
            "class FakePlugin:\n"
            "    async def go(self):\n"
            "        await self.db_manager.definitely_not_a_method()\n"
            "        return self.nonexistent_attr.foo\n"
        )
    }
    violations = find_violations(fake, service_symbols, set())
    assert any("definitely_not_a_method" in v for v in violations), violations
    assert any("nonexistent_attr" in v for v in violations), violations


def test_checker_knows_real_methods():
    """自检：真实存在的方法不应被误报。"""
    service_symbols = load_service_symbols()
    fake = {
        "fake.py": (
            "class FakePlugin:\n"
            "    async def go(self):\n"
            "        await self.db_manager.purge_old_score_history(90)\n"
            "        await self.ladder_service.invalidate_leaderboard_cache('g')\n"
        )
    }
    assert find_violations(fake, service_symbols, set()) == []
