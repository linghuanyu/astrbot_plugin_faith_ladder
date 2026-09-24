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
    """真实的配置读取与录入实现体，只把闸门/权限/身份解析换成固定值。"""

    def __init__(self, db_manager, config=None):
        self.db_manager = db_manager
        self.ladder_service = LadderService(db_manager)
        self.config = config or {}
        self._specific_classes = _load_specific_classes()
        self._sorted_specific_classes = sorted(
            self._specific_classes.items(), key=lambda x: len(x[0]), reverse=True
        )

    async def _gate(self, event, action=None):
        return False, None

    async def _check_perm(self, event):
        return True

    def _get_group_id(self, event):
        return "10001"

    def _get_args(self, event, cmd_name):
        return event.args

    def _parse_card_info(self, card):
        return card_utils.parse_card_info(card, self._sorted_specific_classes)

    async def _get_at_user_id(self, event):
        return event.at_user_id


class _Event:
    """假事件：只需要录入路径用到的那几样。"""

    def __init__(self, args="", at_user_id=None, card="", member_error=None):
        self.args = args
        self.at_user_id = at_user_id
        self.card = card
        self.member_error = member_error

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
