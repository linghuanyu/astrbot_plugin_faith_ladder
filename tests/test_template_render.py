"""
文案池占位符渲染：不许再用 str.format 套诸神可编辑的文案。

为什么值得一整份测试：`prayer_trigger_messages_*` / `wager_messages_*` /
`wish_messages_*` / `oath_text_*` 都是诸神在 WebUI 里能改的，schema 的 hint 还在
教他们写 `{god}`、`{delta}`。而 `str.format` 对这类文本有两个必炸的口子：

- `{player}` 这种写错的占位符 → `KeyError`
- 没闭合的 `{`（想写 markdown 表格、JSON 示例，或单纯手滑）→
  `ValueError: Single '{' encountered in format string`

两条都会从 handler 里抛出去。祷词那条路径的顺序是「第 14 步落库、第 15 步渲染」，
所以异常一抛就是**分数变了但群里一个字都没回**——玩家无从知道发生了什么，
诸神也无从知道是自己的文案写坏了。

改法见 `text_utils.render_template`：白名单正则替换，名单外的花括号原样保留。
"""

import ast
from pathlib import Path

import pytest
import pytest_asyncio

from astrbot_plugin_faith_ladder.text_utils import render_template
from astrbot_plugin_faith_ladder.wish_service import WishService

ROOT = Path(__file__).resolve().parent.parent
PRODUCTION_FILES = sorted(ROOT.glob("*.py")) + sorted((ROOT / "commands").glob("*.py"))

# 这些函数的返回值是"诸神在配置里写的文案"，拿到它们的文本就必须走 render_template
CONFIG_TEXT_SOURCES = {"cfg_get", "pick_wish_line", "pick_wager_line"}


class TestRenderTemplate:
    """纯函数：名单内替换、名单外一个字都不动。"""

    def test_replaces_known_placeholders(self):
        assert render_template("神明{god}赐你{delta}", {"god": "欺诈", "delta": 2}) == "神明欺诈赐你2"

    def test_unknown_placeholder_is_kept_verbatim(self):
        """写错占位符是诸神的自由，但不能把整条消息带走——原样留着让他自己看见。"""
        assert render_template("神明{player}看着你", {"god": "欺诈"}) == "神明{player}看着你"

    def test_known_placeholder_without_value_becomes_empty(self):
        """名单内但值为 None：当空串，不把 {god} 原样发给群友。"""
        assert render_template("神明{god}看着你", {"god": None}) == "神明看着你"

    def test_unclosed_brace_does_not_raise(self):
        """str.format 在这里抛 ValueError；这是本次修复的核心动机。"""
        assert render_template("神明{看着你", {"god": "欺诈"}) == "神明{看着你"

    def test_stray_closing_brace_does_not_raise(self):
        assert render_template("神明}看着你", {}) == "神明}看着你"

    def test_empty_placeholder_does_not_raise(self):
        assert render_template("神明{}看着你", {"god": "欺诈"}) == "神明{}看着你"

    def test_no_double_brace_unescaping(self):
        """已知差异：str.format 会把 `{{` 收成 `{`，这里不做这层转义。

        扫描是自左向右的，所以 `{{god}}` 里真正被替换的是内层的 `{god}`，
        结果 `{欺诈}`——不会像 str.format 那样输出字面的 `{god}`。
        schema 的 hint 教的是 `{god}` 这种写法，没有 `{{` 的用法。
        """
        assert render_template("{{god}}", {"god": "欺诈"}) == "{欺诈}"

    def test_json_example_survives_and_still_fills_placeholders(self):
        """回复里贴 JSON 示例是最常见的"落单花括号"来源：str.format 直接 ValueError。"""
        assert render_template('{"god": "{god}", "分": "{delta}"}', {"god": "欺诈", "delta": 2}) == (
            '{"god": "欺诈", "分": "2"}'
        )

    def test_repeated_placeholder(self):
        assert render_template("{god}与{god}", {"god": "秩序"}) == "秩序与秩序"

    def test_non_string_values_are_stringified(self):
        assert render_template("{delta}", {"delta": -2}) == "-2"

    @pytest.mark.parametrize("blank", [None, ""])
    def test_blank_template_is_safe(self, blank):
        assert render_template(blank, {"god": "欺诈"}) == ""

    def test_missing_values_mapping_is_safe(self):
        assert render_template("{god}", None) == "{god}"


# ── 接入点回归 ───────────────────────────────────────────────────────────────


GROUP = "100"
PLAYER_QQ = "556"
PRAYER = "感孕众生衔育自然"   # schema 默认祷词之一，恰好 8 个汉字

# 固定分值区间（min == max）让 delta 恒为 2：不落在上限，因此走 positive 分支而不是 crit
BASE_CONFIG = {
    "prayer_trigger_groups": [GROUP],
    "prayer_text_诞育": [PRAYER],
    "prayer_score_enabled": True,
    "prayer_score_min": 2,
    "prayer_score_max": 2,
}


class _Sender:
    def __init__(self):
        self.role = "member"


class _MessageObj:
    def __init__(self, group_id):
        self.sender = _Sender()
        self.group_id = group_id


class _Event:
    """祷词触发用的最小群消息事件（与 tests/test_god_praise.py 同款）。"""

    def __init__(self, sender_id, text, group_id=GROUP):
        self._sender_id = sender_id
        self.message_str = text
        self.message_obj = _MessageObj(group_id)
        self.stopped = False

    def get_sender_id(self):
        return self._sender_id

    def plain_result(self, text):
        return text

    def stop_event(self):
        self.stopped = True


@pytest_asyncio.fixture
async def plugin(stubbed_astrbot):
    """起一个真插件（桩掉 astrbot），登记一名只有具体信仰的玩家。"""
    import astrbot_plugin_faith_ladder.main as m

    p = m.FaithLadderPlugin(m.Context(), dict(BASE_CONFIG))
    await p.initialize()
    await p.db_manager.upsert_player(GROUP, "name:张三", "张三")
    await p.db_manager.set_player_specific_faith(GROUP, "name:张三", "诞育")
    await p.db_manager.set_player_qq(GROUP, "name:张三", PLAYER_QQ)
    await p.db_manager.commit()
    try:
        yield p
    finally:
        await p.terminate()


async def _pray(plugin, event):
    return [r async for r in plugin._prayer_message_impl(event)]


async def _pilgrimage(plugin):
    player = await plugin.db_manager.get_player_by_qq(GROUP, PLAYER_QQ)
    return player.pilgrimage_score


class TestPrayerMessageWithBrokenBraces:
    """祷词文案写坏时：分数照旧落库，回复照旧发出——两者都不能少。"""

    @pytest.mark.parametrize(
        "pool_text, must_survive_verbatim",
        [
            # 未闭合的 {：str.format 抛 ValueError: Single '{' encountered
            ("神明{看着你", "神明{看着你"),
            # 写错的占位符：str.format 抛 KeyError
            ("{player} 的神明看着你", "{player} 的神明看着你"),
            # JSON 示例：{" 开头就足够让 str.format 抛 ValueError
            ('{"god": "{god}", "分": "{delta}"}', '"god": "诞育"'),
            # 非 ASCII 的"像占位符的东西"——正则只认 ASCII 标识符，整段原样保留
            ("神明{赐你福泽", "神明{赐你福泽"),
        ],
    )
    async def test_broken_text_still_replies_and_still_writes_score(
        self, plugin, pool_text, must_survive_verbatim
    ):
        before = await _pilgrimage(plugin)
        plugin.config["prayer_trigger_messages_positive"] = [pool_text]

        replies = await _pray(plugin, _Event(PLAYER_QQ, PRAYER))

        assert len(replies) == 1, (
            "文案渲染失败不该让整条回复消失——分数这时已经落库了（commands/prayer.py 第 14/15 步）"
        )
        assert must_survive_verbatim in replies[0], (
            "写坏的花括号应当原样发回来，诸神才知道自己写错了"
        )
        assert await _pilgrimage(plugin) - before == 2, "分数必须照常写入"

    async def test_healthy_text_still_renders_placeholders(self, plugin):
        """正常文案不受影响：占位符照旧替换。"""
        plugin.config["prayer_trigger_messages_positive"] = ["{god}赐你{delta}分"]

        replies = await _pray(plugin, _Event(PLAYER_QQ, PRAYER))

        assert len(replies) == 1
        assert "诞育赐你2分" in replies[0]


class TestWishLineBrokenBraces:
    """祈愿播报走同一条渲染路径（原先的 try/except 漏了 ValueError）。"""

    def _service(self, **config):
        return WishService(None, lambda: config)

    @pytest.mark.parametrize(
        "pool_text, expected",
        [
            # 未闭合的 {：str.format 抛 ValueError，整条播报丢失
            ("神明{看着你", "神明{看着你"),
            # 写错的占位符：str.format 抛 KeyError（原先被兜住，但兜法是原样发未渲染模板）
            ("{查无此人} 的队伍成行了", "{查无此人} 的队伍成行了"),
            # JSON 示例：{" 开头就足够让 str.format 抛 ValueError；内层真占位符照旧替换
            ('{"队": "{team}"}', '{"队": "09月28日祈愿试炼"}'),
        ],
    )
    def test_broken_text_does_not_raise(self, pool_text, expected):
        svc = self._service(wish_messages_open=[pool_text])

        text = svc._line("open", team="09月28日祈愿试炼", leader="张三", count=1,
                         capacity=3, missing=2)

        assert text == expected

    def test_healthy_text_still_renders(self):
        svc = self._service(wish_messages_open=["【{team}】{count}/{capacity}"])

        text = svc._line("open", team="09月28日祈愿试炼", leader="张三", count=1,
                         capacity=3, missing=2)

        assert text == "【09月28日祈愿试炼】1/3"

    def test_unknown_kind_returns_empty(self):
        """池名不认识时 pick_wish_line 返回空串，_line 原样返回空。

        注意：`wish_messages_*` 留空**不是**"没有文案"——schema 默认值是内置池，
        pick_wish_line 会回落到内置池，所以配空串不会让播报消失。
        """
        assert self._service(wish_messages_open=[])._line("open") != ""
        assert self._service()._line("不存在的池") == ""


# ── 静态守卫 ─────────────────────────────────────────────────────────────────


class _FormatOnConfigTextScanner(ast.NodeVisitor):
    """找出对"配置来源文案"使用 str.format 的位置。"""

    def __init__(self, path: Path):
        self.path = path
        self.funcs = ["<module>"]
        self.hits = []

    def visit_FunctionDef(self, node):
        self.funcs.append(node.name)
        self.generic_visit(node)
        self.funcs.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "format":
            reason = self._reason(func.value, node)
            if reason:
                self.hits.append((self.path.name, self.funcs[-1], node.lineno, reason))
        self.generic_visit(node)

    @staticmethod
    def _reason(target, call) -> str:
        """空串 = 放行。只认两种写法，其余（常量文案的具名 format）是合法的。"""
        # 1) .format(**something)：字典展开只出现在"文案池 + 变量字典"的写法里
        if any(kw.arg is None for kw in call.keywords):
            return "用了 .format(**...)，改走 render_template"
        # 2) cfg_get(...) / pick_wish_line(...) / pick_wager_line(...) 的返回值直接 format
        for sub in ast.walk(target):
            if isinstance(sub, ast.Call):
                named = _callee_name(sub.func)
                if named in CONFIG_TEXT_SOURCES:
                    return f"{named}(...) 的返回值直接 .format()，改走 render_template"
        return ""


def _callee_name(func) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _scan():
    hits = []
    for path in PRODUCTION_FILES:
        scanner = _FormatOnConfigTextScanner(path)
        scanner.visit(ast.parse(path.read_text(encoding="utf-8")))
        hits.extend(scanner.hits)
    return hits


def test_no_str_format_on_configurable_text():
    """诸神可编辑的文案一律走 render_template。

    留着 `str.format` 的口子，就迟早会有人写出未闭合的 `{`，而那一条的代价是
    「分数变了但没回话」。`messages.py` 里那些开发者常量可继续用具名 format
    （它们不受用户输入影响，且被测试钉住）。
    """
    offenders = [f"{name}:{lineno}（函数 {func}）{reason}" for name, func, lineno, reason in _scan()]
    assert offenders == [], (
        "这些地方仍在用 str.format 套可配置文案（见 text_utils.render_template）：\n"
        + "\n".join(offenders)
    )
