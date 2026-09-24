"""
文案守卫：把 v3.7.10 清退掉的写法钉死，免得以后改文案时又写回去。

风格对齐 test_symbol_refs / test_config_access：直接扫源码文本 + AST，不依赖运行期。
注释天然不参与扫描（AST 里根本没有注释），docstring 显式排除——
否则解释「为什么不用这个词」的说明文字会把守卫自己顶红。
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 已清退的写法。前两个在原著里 0 次命中（属自造），后两个是散落的多写法统一后的残留。
RETIRED_TERMS = ("命册", "息内", "不存在这个宇宙", "寰宇诸神的注意")

# 「恩主」在原著里只指「玩家自己所奉的那位神」，泛指神明处一律用「神明 / 诸神」。
# 用法收敛到 terms.py 的三个函数后，这个字面量在生产代码里应当只存在于 terms.py。
PATRON = "恩主"
PATRON_LITERAL_ALLOWED_FILES = {"terms.py"}


def _production_sources():
    """生产代码：包根目录的 .py 与 commands/ 下的 .py（不含测试与 __init__）。"""
    files = list(sorted(ROOT.glob("*.py"))) + list(sorted((ROOT / "commands").glob("*.py")))
    return [p for p in files if p.name != "__init__.py"]


def _string_literals(source: str):
    """产出 (行号, 字符串字面量)；docstring 排除，注释本就不在 AST 里。"""
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            yield node.lineno, node.value


def find_retired_terms(source: str):
    """扫出一段源码里命中的已清退写法，返回 [(行号, 词, 文本)]。"""
    hits = []
    for line, text in _string_literals(source):
        for term in RETIRED_TERMS:
            if term in text:
                hits.append((line, term, text))
    return hits


def find_patron_literals(source: str):
    """扫出一段源码里含「恩主」的字符串字面量，返回 [(行号, 文本)]。"""
    return [(line, text) for line, text in _string_literals(source) if PATRON in text]


class TestRetiredTerms:
    def test_no_retired_terms_in_production(self):
        offenders = []
        for path in _production_sources():
            for line, term, text in find_retired_terms(path.read_text(encoding="utf-8")):
                offenders.append(f"{path.name}:{line} 出现「{term}」→ {text}")
        assert not offenders, "已清退的写法又回来了：\n" + "\n".join(offenders)

    def test_guard_catches_a_reintroduced_term(self):
        """故障注入：把清退词写回去，守卫必须变红。"""
        hits = find_retired_terms('MSG = "5 人不在命册，已略过。"\n')
        assert len(hits) == 1
        assert hits[0][1] == "命册"

    def test_comments_and_docstrings_are_ignored(self):
        """说明「为什么不用这个词」的注释与 docstring 不该把守卫顶红。"""
        source = (
            '"""本模块不再使用「命册」这个自造词。"""\n'
            "# 曾经写成 5 人不在命册\n"
            'MSG = "5 人不在本宇宙"\n'
        )
        assert find_retired_terms(source) == []


class TestPatronBoundary:
    def test_patron_literal_only_in_terms(self):
        offenders = []
        for path in _production_sources():
            if path.name in PATRON_LITERAL_ALLOWED_FILES:
                continue
            for line, text in find_patron_literals(path.read_text(encoding="utf-8")):
                offenders.append(f"{path.name}:{line} → {text}")
        assert not offenders, (
            "「恩主」只指玩家自己所奉的神，请改用 terms.py 里的 patron_* 函数：\n"
            + "\n".join(offenders)
        )

    def test_terms_is_the_single_source(self):
        """字面量必须定义在 terms.py，且以 PATRON 常量的形式（不是散在句子里）。"""
        source = (ROOT / "terms.py").read_text(encoding="utf-8")
        assert 'PATRON = "恩主"' in source

    def test_guard_catches_patron_outside_whitelist(self):
        """故障注入：在别的文件里直接写整句「你的恩主…」，应当能被扫出来。"""
        literals = find_patron_literals('MSG = "你的恩主看着你。"\n')
        assert literals == [(1, "你的恩主看着你。")]
