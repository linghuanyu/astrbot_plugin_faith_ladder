"""
录入玩家的参数分类与名片补全。

回归的这条：名片 `【混乱】子琅 渔夫 1000 100` 里的「渔夫」是**混乱·猎人的具体
职业名**，诸神把它照抄进参数时，此前会被当成玩家名、又因为显式名优先于名片解析，
把被 @ 的人静默录成一个叫「渔夫」的玩家（职业/命途由名片补全，校验还能全过）。

做法沿用 tests/test_status_blocks.py：用真实 mixin + 假 event，跑到落库为止。
"""

import json
import sys
import types
from pathlib import Path

import pytest

from astrbot_plugin_faith_ladder import card_utils
from astrbot_plugin_faith_ladder.commands.config import ConfigMixin
from astrbot_plugin_faith_ladder.commands.player import PlayerCommandsMixin
from astrbot_plugin_faith_ladder.ladder_service import LadderService
from astrbot_plugin_faith_ladder.models import FAITH_TO_PATH
from astrbot_plugin_faith_ladder.text_utils import extract_args

SPECIFIC_CLASSES_PATH = Path(__file__).resolve().parent.parent / "specific_classes.json"

# 报障用的真实名片：混乱 的猎人职业叫「渔夫」
REAL_CARD = "【混乱】子琅 渔夫 1000 100"


def _load_specific_classes():
    """按 main.py 的方式构造 (具体职业 → (信仰, 命途, 基础职业)) 映射。"""
    with open(SPECIFIC_CLASSES_PATH, encoding="utf-8") as f:
        data = json.load(f)
    mapping = {}
    for faith, classes in data.items():
        for basic_class, specific_name in classes.items():
            if basic_class == "祷词":
                continue
            mapping[specific_name] = (faith, FAITH_TO_PATH.get(faith), basic_class)
    return mapping


@pytest.fixture
def stubbed_components(monkeypatch):
    """录入成功且带 @ 时会 import At/Plain，桩掉框架依赖（装了 AstrBot 就不用）。"""
    try:
        import astrbot.core.message.components  # noqa: F401
        return
    except ImportError:
        pass

    def _mod(name, **attrs):
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        monkeypatch.setitem(sys.modules, name, module)

    class _Segment:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    _mod("astrbot")
    _mod("astrbot.core")
    _mod("astrbot.core.message")
    _mod("astrbot.core.message.components", At=_Segment, Plain=_Segment)


class _Host(ConfigMixin, PlayerCommandsMixin):
    """真实的配置读取与录入实现体，只把闸门/权限/身份解析换成固定值。

    real_args=True 时 `_get_args` 走真实的 `extract_args`（含 @ 文本剥离），
    用来覆盖「适配器把**整张名片**拼进 message_str」这条线上路径。
    """

    def __init__(self, db_manager, config=None, real_args=False):
        self.db_manager = db_manager
        self.ladder_service = LadderService(db_manager)
        self.config = config or {}
        self._specific_classes = _load_specific_classes()
        self._sorted_specific_classes = sorted(
            self._specific_classes.items(), key=lambda x: len(x[0]), reverse=True
        )
        self._specific_class_prefixes = card_utils.build_specific_class_prefix_index(
            self._sorted_specific_classes
        )
        self._real_args = real_args

    async def _gate(self, event, action=None):
        return False, None

    async def _check_perm(self, event):
        return True

    def _get_group_id(self, event):
        return "10001"

    def _get_args(self, event, cmd_name):
        if not self._real_args:
            return event.args
        return extract_args(event.message_str, cmd_name, event.mentions)

    def _mention_texts(self, event):
        return event.mentions

    async def _find_member_by_name(self, event, player_name):
        """与 main.py 同口径的简版：名片词级匹配、唯一才算（多个匹配返回 None）。"""
        matches = [
            m for m in event.members
            if player_name in card_utils.extract_card_words(
                m.get("card", "") or m.get("nickname", "")
            )
        ]
        return matches[0] if len(matches) == 1 else None

    def _parse_card_info(self, card):
        return card_utils.parse_card_info(
            card, self._sorted_specific_classes, self._specific_class_prefixes
        )

    async def _get_at_user_id(self, event):
        return event.at_user_id


class _Event:
    """假事件：只需要录入路径用到的那几样。"""

    def __init__(self, args="", at_user_id=None, card="", member_error=None,
                 message_str="", mentions=(), members=None):
        self.args = args
        self.at_user_id = at_user_id
        self.card = card
        self.member_error = member_error
        self.message_str = message_str
        self.mentions = list(mentions)
        self.members = members or []

    def get_sender_id(self):
        return "999"

    def plain_result(self, text):
        return text

    def chain_result(self, chain):
        return chain

    @property
    def bot(self):
        return self

    async def get_group_member_info(self, group_id, user_id):
        if self.member_error is not None:
            raise self.member_error
        return {"card": self.card}


async def _run(host, event):
    return [result async for result in host._register_player_impl(event)]


class TestRegisterArgsClassify:
    async def test_specific_class_in_args_is_not_the_name(self, db_manager, stubbed_components):
        """回归：参数里照抄名片的具体职业名「渔夫」，玩家名仍取名片里的「子琅」。"""
        host = _Host(db_manager)
        await _run(host, _Event(args="混沌 渔夫 1000 100", at_user_id="222", card=REAL_CARD))

        player = await db_manager.get_player_by_name("10001", "子琅")
        assert player is not None, "名片里的姓名必须被用上"
        assert player.class_ == "猎人", "具体职业名要归一成基础职业"
        assert player.faith == "混沌"
        assert player.specific_faith == "混乱"
        assert player.ladder_score == 1000
        assert player.pilgrimage_score == 100
        assert await db_manager.get_player_by_name("10001", "渔夫") is None

    async def test_faith_in_args_is_not_the_name(self, db_manager, stubbed_components):
        """参数写具体信仰（繁荣）也一样：只补全信仰，不当玩家名。"""
        host = _Host(db_manager)
        await _run(host, _Event(args="繁荣 1000 100", at_user_id="222", card=REAL_CARD))

        player = await db_manager.get_player_by_name("10001", "子琅")
        assert player is not None
        assert player.specific_faith == "繁荣"
        assert player.faith == "生命", "信仰要推出对应命途"
        assert await db_manager.get_player_by_name("10001", "繁荣") is None

    async def test_path_and_base_class_args_keep_card_name(self, db_manager, stubbed_components):
        host = _Host(db_manager)
        await _run(host, _Event(args="混沌 猎人 1100 50", at_user_id="222", card=REAL_CARD))

        player = await db_manager.get_player_by_name("10001", "子琅")
        assert player is not None
        assert player.class_ == "猎人"
        assert player.faith == "混沌"
        assert player.ladder_score == 1100
        assert player.pilgrimage_score == 50

    async def test_explicit_name_still_overrides_card(self, db_manager, stubbed_components):
        """真的写了姓名时，显式仍然优先（原有约定不变）。"""
        host = _Host(db_manager)
        await _run(host, _Event(args="小明 混沌 猎人", at_user_id="222", card=REAL_CARD))

        assert await db_manager.get_player_by_name("10001", "小明") is not None
        assert await db_manager.get_player_by_name("10001", "子琅") is None

    async def test_without_at_only_args_are_used(self, db_manager, stubbed_components):
        host = _Host(db_manager)
        await _run(host, _Event(args="张三 生命 战士 1200 60"))

        player = await db_manager.get_player_by_name("10001", "张三")
        assert player is not None
        assert player.class_ == "战士"
        assert player.faith == "生命"
        assert player.specific_faith is None
        assert player.ladder_score == 1200
        assert player.pilgrimage_score == 60

    async def test_card_read_failure_is_explained(self, db_manager, stubbed_components):
        """取不到群名片时要说清原因，而不是只报「缺少必要参数」。"""
        host = _Host(db_manager)
        results = await _run(
            host,
            _Event(args="", at_user_id="222", member_error=RuntimeError("permission denied")),
        )

        assert results, "应当有回复"
        text = str(results[0])
        assert "缺少必要参数" in text
        assert "未能读取" in text and "群名片" in text


class TestMentionLeakRegression:
    """线上两次报障的回归：@ 文本剥剩的尾巴不许当玩家名。

    aiocqhttp 适配器把**整张群名片**拼成 "@昵称(QQ)" 写进 message_str，名片带空格
    时只按空白截断会漏下尾巴：9-24 漏成「渔夫」占名字槽，9-26 漏成
    "100(2314617721)" 直接落库成玩家名。
    """

    CARD = "【繁荣】子琅 战士 1000 100"
    QQ = "2314617721"

    async def test_register_by_at_uses_card_name(self, db_manager, stubbed_components):
        host = _Host(db_manager, real_args=True)
        await _run(host, _Event(
            at_user_id=self.QQ,
            card=self.CARD,
            message_str=f"录入玩家 @{self.CARD}({self.QQ})",
            mentions=[(self.CARD, self.QQ)],
        ))

        player = await db_manager.get_player_by_name("10001", "子琅")
        assert player is not None, "名字必须取名片里的「子琅」"
        assert player.class_ == "战士"
        assert player.faith == "生命"
        assert player.specific_faith == "繁荣"
        assert player.qq_id == self.QQ
        assert await db_manager.get_player_by_name("10001", f"100({self.QQ})") is None

    async def test_explicit_args_after_at_are_kept(self, db_manager, stubbed_components):
        """@ 之后写的命途/职业要真的生效（@ 尾巴不许混进参数）。"""
        card = "子琅 战士 1000 100"
        host = _Host(db_manager, real_args=True)
        await _run(host, _Event(
            at_user_id=self.QQ,
            card=card,
            message_str=f"录入玩家 @{card}({self.QQ}) 混沌 渔夫",
            mentions=[(card, self.QQ)],
        ))

        player = await db_manager.get_player_by_name("10001", "子琅")
        assert player is not None
        assert player.faith == "混沌", "参数里的命途要生效（不是名片返回的空）"
        assert player.class_ == "猎人", "参数里的具体职业名要归一成基础职业"
        assert player.specific_faith == "混乱"
        assert await db_manager.get_player_by_name("10001", "渔夫") is None

    async def test_leaked_tail_in_args_is_dropped(self, db_manager, stubbed_components):
        """第二道防线：万一 @ 文本还是漏进参数，"100(QQ)" 也不能当名字。"""
        host = _Host(db_manager)
        await _run(host, _Event(
            args=f"战士 1000 100({self.QQ})", at_user_id=self.QQ, card=self.CARD,
        ))

        player = await db_manager.get_player_by_name("10001", "子琅")
        assert player is not None, "残尾被丢弃后要回落到名片姓名"
        assert await db_manager.get_player_by_name("10001", f"100({self.QQ})") is None

    async def test_9_24_shape_keeps_card_name(self, db_manager, stubbed_components):
        """9-24 形态：名片里的「渔夫」是具体职业名，不能占名字槽。"""
        card = "【混乱】子琅 渔夫 1000 100"
        host = _Host(db_manager, real_args=True)
        await _run(host, _Event(
            at_user_id="2921544554",
            card=card,
            message_str=f"录入玩家 @{card}(2921544554) 虚无 战士",
            mentions=[(card, "2921544554")],
        ))

        player = await db_manager.get_player_by_name("10001", "子琅")
        assert player is not None
        assert player.class_ == "战士"
        assert player.specific_faith == "混乱"
        assert await db_manager.get_player_by_name("10001", "渔夫") is None

    async def test_alias_command_with_args(self, db_manager, stubbed_components):
        """别名调用同样能取到参数（走真实 extract_args）。"""
        host = _Host(db_manager, real_args=True)
        await _run(host, _Event(message_str="添加玩家 张三 生命 战士"))

        assert await db_manager.get_player_by_name("10001", "张三") is not None


class TestCardAbbreviationFromLog:
    """今天第一份日志：名片写简称「子嗣牧」，职业解析不出来、还被粘进姓名。"""

    async def test_abbreviated_specific_class_registers(self, db_manager, stubbed_components):
        card = "【诞育】棉絮 子嗣牧 1000 100"
        host = _Host(db_manager, real_args=True)
        await _run(host, _Event(
            at_user_id="1614608495",
            card=card,
            message_str=f"录入玩家 @{card}(1614608495)",
            mentions=[(card, "1614608495")],
        ))

        player = await db_manager.get_player_by_name("10001", "棉絮")
        assert player is not None, "简称不该被粘进姓名（此前会录成「棉絮子嗣牧」）"
        assert player.class_ == "牧师", "「子嗣牧」要认出是「子嗣牧师」"
        assert player.faith == "生命"
        assert player.specific_faith == "诞育"


class TestMissingParamsHint:
    async def test_plain_name_card_says_what_to_send(self, db_manager, stubbed_components):
        """只写名字的名片：仍报缺参数（按既定口径），但要给出可照抄的补参命令。"""
        card = "Hyperthymesia"
        host = _Host(db_manager, real_args=True)
        results = await _run(host, _Event(
            at_user_id="3800864398",
            card=card,
            message_str=f"录入玩家 @{card}(3800864398)",
            mentions=[(card, "3800864398")],
        ))

        text = str(results[0])
        assert "缺少必要参数" in text
        assert "命途" in text and "职业" in text
        assert "补一次即可" in text
        assert "录入玩家 @用户 <命途> <职业>" in text


class TestManualRegisterNotifies:
    """无 @ 录入成功时也要 @ 到对应玩家（按名字在群成员里找唯一匹配）。"""

    async def test_matching_member_is_atted(self, db_manager, stubbed_components):
        host = _Host(db_manager)
        results = await _run(host, _Event(
            args="张三 生命 战士",
            members=[{"user_id": 555, "card": "【生命】张三"}],
        ))

        chain = results[0]
        assert isinstance(chain, list) and chain, "成功回复应当是 [@目标, 文案]"
        assert any(getattr(seg, "qq", None) == 555 for seg in chain)

    async def test_ambiguous_member_falls_back_to_plain(self, db_manager, stubbed_components):
        host = _Host(db_manager)
        results = await _run(host, _Event(
            args="张三 生命 战士",
            members=[
                {"user_id": 555, "card": "张三"},
                {"user_id": 556, "card": "张三 战士"},
            ],
        ))

        assert isinstance(results[0], str), "多个同名成员时不 @，退回纯文本"


class TestNoCardManualFallback:
    """对方没有群名片（或机器人读不到名片）时：姓名/命途/职业都在参数里补，@ 照旧绑 QQ。"""

    async def test_no_card_text_at_all(self, db_manager, stubbed_components):
        """适配器连 @ 文本都没拼（拿不到任何昵称）时，参数里的名字必须生效。"""
        host = _Host(db_manager, real_args=True)
        await _run(host, _Event(
            at_user_id="2314617721",
            card="",
            message_str="录入玩家 张三 命运 战士",
            mentions=[("", "2314617721")],
        ))

        player = await db_manager.get_player_by_name("10001", "张三")
        assert player is not None
        assert player.faith == "虚无" and player.specific_faith == "命运"
        assert player.class_ == "战士"
        assert player.qq_id == "2314617721", "@ 仍然要把 QQ 绑上"

    async def test_card_read_error_with_full_args(self, db_manager, stubbed_components):
        """取名片抛异常（没权限/对方已退群）也要能用参数补全录入。"""
        host = _Host(db_manager, real_args=True)
        await _run(host, _Event(
            at_user_id="2314617721",
            member_error=RuntimeError("permission denied"),
            message_str="录入玩家 @李四(2314617721) 李四 生命 牧师",
            mentions=[("李四", "2314617721")],
        ))

        player = await db_manager.get_player_by_name("10001", "李四")
        assert player is not None
        assert player.faith == "生命" and player.class_ == "牧师"
        assert player.qq_id == "2314617721"


class TestSpecificFaithArgToPath:
    async def test_fate_maps_to_void_path(self, db_manager, stubbed_components):
        """写具体信仰「命运」→ 命途自动是「虚无」（v3.9.0 起的行为，钉死）。"""
        host = _Host(db_manager)
        await _run(host, _Event(args="张三 命运 战士 1000 100"))

        player = await db_manager.get_player_by_name("10001", "张三")
        assert player is not None
        assert player.specific_faith == "命运"
        assert player.faith == "虚无"
