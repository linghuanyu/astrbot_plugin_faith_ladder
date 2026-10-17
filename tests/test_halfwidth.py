"""
全角折叠（NFKC）：只用在真正会失败的地方，别扩大范围。

这份用例同时钉住**实测出来的边界**，因为"全角要不要处理"很容易凭直觉判断错：

- `int('１２３')` 本来就是 `123`，`'１２３'.isdigit()` 也是 `True`——
  Python 的 `int()` 与字符串判断直接接受 Unicode 数字，**全角数字从来不是 bug**。
  正则里的 `\\d` 同样直接匹配全角数字。
- 真正会抛 `ValueError` 的是**全角正负号**：`int('＋16')`、`int('－5')`。
  这是"诸神以为输了分数、插件其实没收到"的静默失败源头。

所以 `to_halfwidth` 的适用面只有两处：带符号的整数 token、以及枚举清单的分隔符。
"""

import ast
from pathlib import Path

import pytest

from astrbot_plugin_faith_ladder.commands.gate import parse_block_actions
from astrbot_plugin_faith_ladder.ladder_service import LadderService
from astrbot_plugin_faith_ladder.text_utils import to_halfwidth

ROOT = Path(__file__).resolve().parent.parent
PRODUCTION_FILES = sorted(ROOT.glob("*.py")) + sorted((ROOT / "commands").glob("*.py"))


class TestToHalfwidth:
    def test_folds_fullwidth_punctuation_and_space(self):
        assert to_halfwidth("，") == ","
        assert to_halfwidth("：") == ":"
        assert to_halfwidth("＋") == "+"
        assert to_halfwidth("－") == "-"
        assert to_halfwidth("　") == " "        # 全角空格 U+3000

    def test_folds_fullwidth_digits(self):
        """数字折叠只是顺手，int() 本来就吃全角数字——见 TestIntAlreadyAcceptsFullwidthDigits。"""
        assert to_halfwidth("１２３") == "123"

    @pytest.mark.parametrize("kept", ["【", "】", "→", "、", "。", "·", "《", "》"])
    def test_cjk_punctuation_is_left_alone(self, kept):
        """NFKC 只处理"兼容等价"，不做别的：这些不变，所以名片解析与
        「旧名 → 新名」的分隔符不受影响，全角顿号也不会变成逗号分隔符。"""
        assert to_halfwidth(kept) == kept

    def test_chinese_text_unchanged(self):
        assert to_halfwidth("不辨真伪勿论虚实") == "不辨真伪勿论虚实"

    @pytest.mark.parametrize("blank", [None, ""])
    def test_blank_is_safe(self, blank):
        assert to_halfwidth(blank) == ""


class TestIntAlreadyAcceptsFullwidthDigits:
    """把"为什么不需要到处折全角"写成可执行的说明，省得下次又有人加一圈无用调用。"""

    def test_fullwidth_digits_parse_fine(self):
        assert int("１２３") == 123
        assert int("+１２３") == 123          # ASCII 加号 + 全角数字
        assert int("-１２３") == -123
        assert "１２３".isdigit() is True

    def test_fullwidth_sign_raises(self):
        with pytest.raises(ValueError):
            int("＋１２３")
        with pytest.raises(ValueError):
            int("－１２３")


class TestBlockActionsFullwidth:
    """阻断项清单：整串折半角，取代原先 ad-hoc 的 replace("，", ",")。

    合法动作名见 commands/gate.py 的 ACTION_LABELS：祷词/赠送/接受赠送/查询/祈愿。
    """

    def test_fullwidth_comma(self):
        stored, unknown = parse_block_actions("祷词，赠送")
        assert unknown == []
        assert stored == "prayer,gift_send"

    def test_fullwidth_space_around_items(self):
        stored, unknown = parse_block_actions("祷词　，　赠送")
        assert unknown == []
        assert stored == "prayer,gift_send"

    def test_ideographic_comma_is_not_a_separator(self):
        """顿号不在 NFKC 的处理范围内，本来也不是分隔符——钉住这个既有口径。"""
        _, unknown = parse_block_actions("祷词、赠送")
        assert unknown == ["祷词、赠送"]

    def test_ascii_comma_still_works(self):
        stored, unknown = parse_block_actions("祷词,赠送")
        assert unknown == []
        assert stored == "prayer,gift_send"


class TestBatchEntryFullwidthSign:
    """批量录入：`【登神之路＋16】` 以前整条正则不匹配，被静默当成 0。"""

    @pytest.fixture
    def service(self):
        return LadderService.__new__(LadderService)

    def test_fullwidth_plus_is_positive(self, service):
        results, err = service.parse_batch_scores("【玩家：张三】\n【登神之路＋16】\n【觐见之梯＋2】")
        assert err is None
        assert results[0]["ladder_delta"] == 16
        assert results[0]["pilgrimage_delta"] == 2

    def test_fullwidth_minus_is_negative(self, service):
        results, err = service.parse_batch_scores("【玩家：张三】\n【登神之路－16】\n【觐见之梯－2】")
        assert err is None
        assert results[0]["ladder_delta"] == -16
        assert results[0]["pilgrimage_delta"] == -2

    def test_fullwidth_digits_still_parse(self, service):
        """全角数字由 `\\d` 与 int() 直接吃下，这条只是防止将来把正则收窄。"""
        results, err = service.parse_batch_scores("【玩家：张三】\n【登神之路 １６】")
        assert err is None
        assert results[0]["ladder_delta"] == 16

    def test_sign_char_class_does_not_swallow_digits(self, service):
        """正则陷阱：字符类里的连字符放错位置会变成区间。

        `[+-＋－]` 会被解析成「`+`(U+002B) 到 `＋`(U+FF0B) 的区间」，
        而这个区间**覆盖了全部 ASCII 数字**，于是 ` 16` 的 `1` 被吃成符号、
        解析出 6（不是 16）。连字符必须放首位。这条被
        `tests/test_review_fixes.py::test_sign_is_optional` 逮到过一次，
        这里再单独钉一遍根因。
        """
        results, err = service.parse_batch_scores("【玩家：张三】\n【登神之路 16】\n【觐见之梯 2】")
        assert err is None
        assert results[0]["ladder_delta"] == 16
        assert results[0]["pilgrimage_delta"] == 2

    def test_ascii_signs_unchanged(self, service):
        results, err = service.parse_batch_scores("【玩家：张三】\n【登神之路+16】\n【觐见之梯-2】")
        assert err is None
        assert results[0]["ladder_delta"] == 16
        assert results[0]["pilgrimage_delta"] == -2


# ── 端到端：指令参数里的全角符号 ─────────────────────────────────────────────


GROUP = "100"
ADMIN = "999"


class _Sender:
    def __init__(self):
        self.role = "member"


class _MessageObj:
    def __init__(self, group_id):
        self.sender = _Sender()
        self.group_id = group_id


class _Event:
    def __init__(self, sender_id, text, group_id=GROUP):
        self._sender_id = sender_id
        self.message_str = text
        self.message_obj = _MessageObj(group_id)
        self.stopped = False

    def get_sender_id(self):
        return self._sender_id

    def get_group_id(self):
        return GROUP

    def get_self_id(self):
        return "1"

    def get_messages(self):
        """@ 剥离要读消息段；给空列表，省掉 main.py 那条"解析 @ 文本失败"的告警。"""
        return []

    def plain_result(self, text):
        return text

    def stop_event(self):
        self.stopped = True


@pytest.fixture
def plugin_factory(stubbed_astrbot):
    import contextlib

    @contextlib.asynccontextmanager
    async def _make(**overrides):
        import astrbot_plugin_faith_ladder.main as m

        config = {"admin_ids": [ADMIN], "group_access_mode": "off", **overrides}
        p = m.FaithLadderPlugin(m.Context(), config)
        await p.initialize()
        await p.db_manager.upsert_player(GROUP, "name:张三", "张三")
        await p.db_manager.commit()
        try:
            yield p
        finally:
            await p.terminate()

    return _make


class TestScoreEntryFullwidthSign:
    """`录入积分 张三 ＋16 0`：以前回「分数必须是整数」，诸神完全看不出是全角的问题。

    分数是**增量**：新玩家从 schema 默认的初始分（1000）起步，所以断言用前后差值，
    免得耦合到 `init_ladder_score` 的默认值上。
    """

    async def _ladder_score(self, p, name="张三"):
        player = await p.db_manager.get_player_by_name(GROUP, name)
        return player.ladder_score

    async def test_fullwidth_sign_is_accepted(self, plugin_factory):
        async with plugin_factory() as p:
            before = await self._ladder_score(p)

            replies = [r async for r in p._add_score_impl(_Event(ADMIN, "录入积分 张三 ＋16 0"))]

            assert replies, "不该回错误提示"
            assert "分数必须是整数" not in replies[0]
            assert await self._ladder_score(p) - before == 16

    async def test_fullwidth_minus_is_accepted(self, plugin_factory):
        async with plugin_factory(allow_negative_scores=True) as p:
            before = await self._ladder_score(p)

            replies = [r async for r in p._add_score_impl(_Event(ADMIN, "录入积分 张三 －5 0"))]

            assert "分数必须是整数" not in replies[0]
            assert await self._ladder_score(p) - before == -5

    async def test_ascii_sign_still_works(self, plugin_factory):
        async with plugin_factory() as p:
            before = await self._ladder_score(p)

            async for _ in p._add_score_impl(_Event(ADMIN, "录入积分 张三 +16 0")):
                pass

            assert await self._ladder_score(p) - before == 16


class TestRegisterArgsFullwidthSign:
    """`录入玩家 李四 诞育 猎人 ＋500 100`：分数位以前认不出全角符号，会被当成名字候选。"""

    async def test_fullwidth_sign_in_register_args(self, plugin_factory):
        async with plugin_factory() as p:
            replies = [
                r async for r in p._register_player_impl(
                    _Event(ADMIN, "录入玩家 李四 诞育 猎人 ＋500 100")
                )
            ]

            assert replies
            player = await p.db_manager.get_player_by_name(GROUP, "李四")
            assert player is not None, f"没录上，回复是：{replies}"
            assert player.ladder_score == 500
            assert player.pilgrimage_score == 100


class TestNamesAreNotFolded:
    """反向守卫：折叠**不能**用到名字上。

    玩家名是精确等值查询（含全角字符的名字原样入库），把整段参数折一遍会让
    这类玩家查不到人。这条用例就是拦住"图省事把 to_halfwidth 套到整段文本"。
    """

    async def test_fullwidth_name_still_resolves_exactly(self, plugin_factory):
        FULLWIDTH_NAME = "Ｑ仔"
        async with plugin_factory() as p:
            await p.db_manager.upsert_player(GROUP, f"name:{FULLWIDTH_NAME}", FULLWIDTH_NAME)
            await p.db_manager.commit()
            before = (await p.db_manager.get_player_by_name(GROUP, FULLWIDTH_NAME)).ladder_score

            replies = [
                r async for r in p._add_score_impl(_Event(ADMIN, f"录入积分 {FULLWIDTH_NAME} ＋7 0"))
            ]

            assert replies
            player = await p.db_manager.get_player_by_name(GROUP, FULLWIDTH_NAME)
            assert player.ladder_score - before == 7, "全角名字必须原样命中；折过就查不到了"
            # 而折过的那个名字应该是查不到的——证明库里存的确实是全角原样
            assert await p.db_manager.get_player_by_name(GROUP, "Q仔") is None


class TestNoScatteredNormalization:
    """静态守卫：NFKC 只允许在 text_utils.to_halfwidth 里出现。"""

    def test_unicodedata_normalize_only_in_text_utils(self):
        offenders = []
        for path in PRODUCTION_FILES:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "normalize"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "unicodedata"
                    and path.name != "text_utils.py"
                ):
                    offenders.append(f"{path.name}:{node.lineno}")

        assert offenders == [], (
            "NFKC 折叠请走 text_utils.to_halfwidth（它的 docstring 写明了适用范围），"
            "散落各处迟早会折到玩家名上：\n" + "\n".join(offenders)
        )
