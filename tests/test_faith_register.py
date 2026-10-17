"""
「信仰注册」：玩家凭自己的群名片自助录入。

语义与「录入玩家 @用户」一致（同一套名片解析、同一个服务层落库路径），区别只有三点：
1. 不需要诸神权限——名片是发送者自己的，记录也只绑定发送者自己的 QQ
2. 不接受参数、分数固定取初始值（否则人人可自填任意分数）
3. 已有记录一律不接管（弱身份不能产生换绑/抢占这类持久副作用）

做法沿用 tests/test_register_args.py：真实 mixin + 假 event，跑到落库为止。
"""

import ast
from pathlib import Path

import pytest

from astrbot_plugin_faith_ladder import card_utils
from astrbot_plugin_faith_ladder.commands.config import ConfigMixin
from astrbot_plugin_faith_ladder.commands.player import PlayerCommandsMixin
from astrbot_plugin_faith_ladder.ladder_service import LadderService
from astrbot_plugin_faith_ladder.models import FAITH_TO_PATH
from astrbot_plugin_faith_ladder.text_utils import extract_args

ROOT = Path(__file__).resolve().parents[1]
SPECIFIC_CLASSES_PATH = ROOT / "specific_classes.json"

QQ = "2314617721"
CARD = "【繁荣】子琅 战士 1000 100"


def _load_specific_classes():
    import json

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
    """成功回复是 [At, Plain] 消息链；无 AstrBot 环境时桩掉这两个组件。"""
    try:
        import astrbot.core.message.components  # noqa: F401
        return
    except ImportError:
        pass

    import sys
    import types

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
    def __init__(self, db_manager, config=None):
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
        self.gate_calls = []

    async def _gate(self, event, action=None):
        self.gate_calls.append(action)
        return self._gate_result

    async def _check_perm(self, event):
        raise AssertionError("信仰注册不该调用权限判定（对所有人开放）")

    def _get_group_id(self, event):
        return "10001"

    def _get_args(self, event, cmd_name):
        return extract_args(event.message_str, cmd_name, event.mentions)

    def _mention_texts(self, event):
        return event.mentions

    def _parse_card_info(self, card):
        return card_utils.parse_card_info(
            card, self._sorted_specific_classes, self._specific_class_prefixes
        )

    _gate_result = (False, None)


class _Event:
    def __init__(self, card="", message_str="", sender=QQ, member_error=None,
                 mentions=()):
        self.card = card
        self.message_str = message_str
        self.sender = sender
        self.member_error = member_error
        self.mentions = list(mentions)
        self.stopped = False

    def get_sender_id(self):
        return self.sender

    def plain_result(self, text):
        return text

    def chain_result(self, chain):
        return chain

    def stop_event(self):
        self.stopped = True

    @property
    def bot(self):
        return self

    async def get_group_member_info(self, group_id, user_id):
        if self.member_error is not None:
            raise self.member_error
        return {"card": self.card}


async def _run(host, event):
    return [r async for r in host._register_faith_impl(event)]


class TestFaithRegisterSuccess:
    async def test_card_creates_player_bound_to_sender(self, db_manager, stubbed_components):
        host = _Host(db_manager)
        replies = await _run(host, _Event(card=CARD, message_str="信仰注册"))

        assert replies, "应当有回复"
        player = await db_manager.get_player_by_name("10001", "子琅")
        assert player is not None, "名片里的姓名要落库"
        assert player.class_ == "战士"
        assert player.faith == "生命"
        assert player.specific_faith == "繁荣"
        assert player.qq_id == QQ, "记录必须绑定发送者自己的 QQ"
        assert player.ladder_score == 1000
        assert player.pilgrimage_score == 100

    async def test_reply_ats_the_sender(self, db_manager, stubbed_components):
        host = _Host(db_manager)
        replies = await _run(host, _Event(card=CARD, message_str="信仰注册"))

        chain = replies[0]
        assert isinstance(chain, list) and chain, "成功回复应当是 [@本人, 文案]"
        assert getattr(chain[0], "qq", None) == int(QQ)

    async def test_specific_class_name_on_card(self, db_manager, stubbed_components):
        host = _Host(db_manager)
        await _run(host, _Event(card="【混乱】子琅 渔夫 1000 100", message_str="信仰注册"))

        player = await db_manager.get_player_by_name("10001", "子琅")
        assert player is not None
        assert player.class_ == "猎人", "具体职业名要归一成基础职业"
        assert player.specific_faith == "混乱"
        assert await db_manager.get_player_by_name("10001", "渔夫") is None

    async def test_abbreviated_class_on_card(self, db_manager, stubbed_components):
        host = _Host(db_manager)
        await _run(host, _Event(card="【诞育】棉絮 子嗣牧 1000 100", message_str="信仰注册"))

        player = await db_manager.get_player_by_name("10001", "棉絮")
        assert player is not None
        assert player.class_ == "牧师"
        assert player.specific_faith == "诞育"

    async def test_gate_is_consulted(self, db_manager, stubbed_components):
        """群访问控制/功能开关的道闸不能绕过（与其它指令一致）。"""
        host = _Host(db_manager)
        await _run(host, _Event(card=CARD, message_str="信仰注册"))
        assert host.gate_calls == [None]


class TestFaithRegisterGate:
    async def test_silent_block_writes_nothing(self, db_manager, stubbed_components):
        host = _Host(db_manager)
        host._gate_result = (True, None)
        replies = await _run(host, _Event(card=CARD, message_str="信仰注册"))

        assert replies == []
        assert await db_manager.get_player_by_name("10001", "子琅") is None

    async def test_gate_message_is_relayed(self, db_manager, stubbed_components):
        host = _Host(db_manager)
        host._gate_result = (True, "本群未启用")
        replies = await _run(host, _Event(card=CARD, message_str="信仰注册"))

        assert replies == ["本群未启用"]
        assert await db_manager.get_player_by_name("10001", "子琅") is None


class TestFaithRegisterRejections:
    async def test_no_arguments_accepted(self, db_manager, stubbed_components):
        """参数一律不认：否则分数可以自填。"""
        host = _Host(db_manager)
        replies = await _run(
            host, _Event(card=CARD, message_str="信仰注册 子琅 生命 战士 9999 9999")
        )

        text = str(replies[0])
        assert "只认群名片" in text
        assert await db_manager.get_player_by_name("10001", "子琅") is None
        assert await db_manager.get_player_by_name("10001", "子琅 生命 战士 9999 9999") is None

    async def test_card_missing_faith_and_class(self, db_manager, stubbed_components):
        host = _Host(db_manager)
        replies = await _run(host, _Event(card="Hyperthymesia", message_str="信仰注册"))

        text = str(replies[0])
        assert "不符合录入格式" in text
        assert "命途" in text and "职业" in text
        assert "格式示例" in text
        assert await db_manager.get_player_by_name("10001", "Hyperthymesia") is None

    async def test_empty_card(self, db_manager, stubbed_components):
        host = _Host(db_manager)
        replies = await _run(host, _Event(card="", message_str="信仰注册"))

        assert "群名片是空的" in str(replies[0])

    async def test_card_read_failure_is_explained(self, db_manager, stubbed_components):
        host = _Host(db_manager)
        replies = await _run(host, _Event(
            message_str="信仰注册", member_error=RuntimeError("permission denied"),
        ))

        text = str(replies[0])
        assert "未能读取" in text and "群名片" in text
        assert "permission denied" in text

    async def test_name_too_long(self, db_manager, stubbed_components):
        host = _Host(db_manager, config={"player_name_max_length": 2})
        replies = await _run(host, _Event(card="【繁荣】三个字 战士", message_str="信仰注册"))

        assert "过长" in str(replies[0])
        assert await db_manager.get_player_by_name("10001", "三个字") is None


class TestExistingRecordsAreNotTakenOver:
    """弱身份（群名片）不得产生换绑/抢占：已有记录一律不接管。"""

    async def _seed(self, db_manager, qq_id=None):
        from astrbot_plugin_faith_ladder.ladder_service import LadderService

        service = LadderService(db_manager)
        ok, _ = await service.register_player(
            "10001", "子琅", "生命", "战士", 1000, 100, "999", qq_id=qq_id,
        )
        assert ok

    async def test_sender_qq_already_bound(self, db_manager, stubbed_components):
        await self._seed(db_manager, qq_id=QQ)
        host = _Host(db_manager)
        replies = await _run(host, _Event(card=CARD, message_str="信仰注册"))

        text = str(replies[0])
        assert "无需重复注册" in text
        assert "子琅" in text

    async def test_same_name_unbound_is_not_taken_over(self, db_manager, stubbed_components):
        """同名记录没有 QQ：不能借名片把它绑到自己身上（那是冒充面）。"""
        await self._seed(db_manager, qq_id=None)
        host = _Host(db_manager)
        replies = await _run(host, _Event(card=CARD, message_str="信仰注册"))

        text = str(replies[0])
        assert "不会接管已有记录" in text
        player = await db_manager.get_player_by_name("10001", "子琅")
        assert player.qq_id is None, "名片路径不得绑定他人记录"

    async def test_same_name_bound_to_other_qq(self, db_manager, stubbed_components):
        await self._seed(db_manager, qq_id="555")
        host = _Host(db_manager)
        replies = await _run(host, _Event(card=CARD, message_str="信仰注册"))

        text = str(replies[0])
        assert "已被其他 QQ 绑定" in text
        player = await db_manager.get_player_by_name("10001", "子琅")
        assert player.qq_id == "555"


class TestRegistration:
    def _command_decorators(self):
        tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                func = dec.func
                if isinstance(func, ast.Attribute) and func.attr == "command" and dec.args:
                    names = [a.value for a in dec.args if isinstance(a, ast.Constant)]
                    aliases = [
                        elt.value
                        for kw in dec.keywords if kw.arg == "alias" and isinstance(kw.value, ast.Set)
                        for elt in kw.value.elts if isinstance(elt, ast.Constant)
                    ]
                    found.append((node.name, names, aliases))
        return found

    def test_registered_in_main_with_aliases(self):
        entries = [e for e in self._command_decorators() if "信仰注册" in e[1]]
        assert entries, "main.py 里找不到「信仰注册」的注册"
        _, _, aliases = entries[0]
        assert "faithregister" in aliases

    def test_does_not_collide_with_register_player(self):
        """「信仰注册」不会被「录入玩家」抢走（上游只认整串相等或「指令 + 空格」）。"""
        message = "信仰注册"
        assert not (message == "录入玩家" or message.startswith("录入玩家 "))
        assert message.startswith("信仰注册")

    def test_help_mentions_command(self):
        from astrbot_plugin_faith_ladder.message_formatter import format_help

        assert "信仰注册" in format_help({})

    def test_config_key_declared(self):
        from astrbot_plugin_faith_ladder.plugin_config import schema_has

        assert schema_has("cmd_register_faith")

    def test_command_prefix_is_registered_for_prayer_filter(self):
        """没进指令前缀集合的话，祷词触发会把「信仰注册」当祷词处理。"""
        from astrbot_plugin_faith_ladder.commands.config import ConfigMixin
        from astrbot_plugin_faith_ladder.commands.prayer import PrayerCommandsMixin
        from astrbot_plugin_faith_ladder.plugin_config import schema_keys

        keys = set(schema_keys())
        source = (ROOT / "commands" / "prayer.py").read_text(encoding="utf-8")
        assert '"cmd_register_faith"' in source
        assert "cmd_register_faith" in keys

        class _PrayerHost(ConfigMixin, PrayerCommandsMixin):
            pass

        # 指令前缀集合是从这些键拼出来的，包含它就能过滤
        host = _PrayerHost()
        host.config = {}
        host._build_prayer_cache()
        assert "信仰注册" in host._command_prefixes
