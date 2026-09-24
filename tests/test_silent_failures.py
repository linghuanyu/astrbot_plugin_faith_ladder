"""
静态守卫：生产代码里不许再出现"吞掉异常且不留任何痕迹"的处理器。

为什么值得一条测试：这个插件被静默失败咬过不止一次——

- `initialize()` 里指向已删除方法的调用，让调度器数个版本没启动，而 365 个用例全绿；
- `get_group_member_info` 失败被 `pass` 吞掉，用户只看到"缺少必要参数"，
  看不出是根本没读到群名片；
- 撤回、自动识别自己、注册文案渲染等路径一旦静默降级，表现都只是
  "某个功能好像没生效"，没有任何日志可查。

规则：`except Exception:`（且只捕获这一个泛型异常）的处理器体里只有 `pass`
就是违规。窄异常（`ValueError`/`OSError`/`CancelledError` 等）属于正常控制流，
不在范围内；确实要静默的地方进 ALLOWED，并写明理由。
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 生产文件：插件根目录下的全部模块 + commands/ 包（tests/ 不在其中）
PRODUCTION_FILES = sorted(ROOT.glob("*.py")) + sorted((ROOT / "commands").glob("*.py"))

# 允许静默的位置：(文件相对路径, 所在函数名) -> 理由
ALLOWED = {
    ("db_manager.py", "rollback"): (
        "最佳努力回滚：调用方已经记过原始异常，这里再记一次就是重复日志；"
        "而且连接已损坏时继续调用日志接口未必安全。"
    ),
    ("qq_admin_handle.py", "try_delete"): (
        "撤回多条历史消息时单条失败属预期（消息可能已被撤回或权限不足），"
        "该指令本身就是「成功即静默」语义，见函数 docstring。"
    ),
}


class _BarePassScanner(ast.NodeVisitor):
    """找出「只捕获 Exception、且体里只有 pass」的处理器，并记下所在函数。"""

    def __init__(self, path: Path):
        self.path = path
        self.funcs = ["<module>"]
        self.hits = []

    def visit_FunctionDef(self, node):
        self.funcs.append(node.name)
        self.generic_visit(node)
        self.funcs.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ExceptHandler(self, node):
        if self._is_bare_exception(node.type) and self._is_only_pass(node.body):
            self.hits.append((self.path.name, self.funcs[-1], node.lineno))
        self.generic_visit(node)

    @staticmethod
    def _is_bare_exception(node) -> bool:
        """只认 `except Exception:`（元组与别名不算，窄异常不在守卫范围）。"""
        return isinstance(node, ast.Name) and node.id == "Exception"

    @staticmethod
    def _is_only_pass(body) -> bool:
        """docstring / 纯字符串表达式不算"留了痕迹"，其余语句算。"""
        meaningful = [
            stmt for stmt in body
            if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))
        ]
        return len(meaningful) == 1 and isinstance(meaningful[0], ast.Pass)


def _scan():
    hits = []
    for path in PRODUCTION_FILES:
        scanner = _BarePassScanner(path)
        scanner.visit(ast.parse(path.read_text(encoding="utf-8")))
        hits.extend(scanner.hits)
    return hits


def test_no_bare_exception_pass_in_production():
    """生产代码不得静默吞掉任意异常：要么记日志，要么进 ALLOWED 并写明理由。"""
    offenders = [
        f"{name}:{lineno}（函数 {func}）"
        for name, func, lineno in _scan()
        if (name, func) not in ALLOWED
    ]
    assert offenders == [], (
        "这些地方吞掉了任意异常且不留任何痕迹（改成 logger.warning/debug，"
        "或加进 ALLOWED 并写明理由）：\n" + "\n".join(offenders)
    )


def test_allowlist_entries_are_still_needed():
    """例外不许留死条目：代码修好后必须把 ALLOWED 里对应的项删掉。"""
    live = {(name, func) for name, func, _ in _scan()}
    stale = [key for key in ALLOWED if key not in live]
    assert stale == [], (
        "这些例外已经不再对应任何静默处理器，请从 ALLOWED 里删掉：\n"
        + "\n".join(f"{name}::{func}" for name, func in stale)
    )
