"""
群名片解析的单元测试。

这段逻辑原先只能通过插件实例调用、测试碰不到，而它恰好最容易出错：
三种信仰来源的优先级、具体职业名的前缀匹配（"魔术师1218"）、
非关键词与玩家名的划分。抽成 card_utils 后可以直接构造名片文本断言。
"""

import json
from pathlib import Path

import pytest

from astrbot_plugin_faith_ladder import card_utils

SPECIFIC_CLASSES_PATH = Path(__file__).resolve().parent.parent / "specific_classes.json"


@pytest.fixture(scope="module")
def sorted_classes():
    """按 main.py 的方式构造 (具体职业 → (信仰, 命途, 基础职业)) 映射，长度降序。"""
    from astrbot_plugin_faith_ladder.models import FAITH_TO_PATH

    with open(SPECIFIC_CLASSES_PATH, encoding="utf-8") as f:
        data = json.load(f)
    mapping = {}
    for faith, classes in data.items():
        for basic_class, specific_name in classes.items():
            if basic_class == "祷词":
                continue
            mapping[specific_name] = (faith, FAITH_TO_PATH.get(faith), basic_class)
    return sorted(mapping.items(), key=lambda x: len(x[0]), reverse=True)


def parse(card, sorted_classes):
    return card_utils.parse_card_info(card, sorted_classes)


class TestParseCardInfo:
    def test_empty_card(self, sorted_classes):
        assert parse("", sorted_classes) == {
            "specific_faith": None, "faith": None, "class_": None, "player_name": None,
        }

    def test_plain_name_only(self, sorted_classes):
        r = parse("张三", sorted_classes)
        assert r["player_name"] == "张三"
        assert r["faith"] is None and r["specific_faith"] is None and r["class_"] is None

    def test_bracket_tag_is_specific_faith(self, sorted_classes):
        """【繁荣】标签 → 具体信仰=繁荣，命途由映射推出（生命）。"""
        r = parse("【繁荣】战士 张三", sorted_classes)
        assert r["specific_faith"] == "繁荣"
        assert r["faith"] == "生命"
        assert r["class_"] == "战士"
        assert r["player_name"] == "张三"

    def test_bracket_tag_with_bracketed_name(self, sorted_classes):
        """名片形如【繁荣】张三（标签后直接跟名字）。"""
        r = parse("【繁荣】张三", sorted_classes)
        assert r["specific_faith"] == "繁荣"
        assert r["player_name"] == "张三"

    def test_specific_class_implies_faith(self, sorted_classes):
        """具体职业自带信仰：酋长 → 诞育 / 生命，职业归一为战士。"""
        r = parse("酋长 王五", sorted_classes)
        assert r["class_"] == "战士"
        assert r["specific_faith"] == "诞育"
        assert r["faith"] == "生命"
        assert r["player_name"] == "王五"

    def test_specific_class_with_digit_suffix(self, sorted_classes):
        """具体职业后紧跟数字（广告昵称常见），数字不入玩家名。"""
        r = parse("酋长1218", sorted_classes)
        assert r["class_"] == "战士"
        assert r["specific_faith"] == "诞育"
        assert r["player_name"] is None

    def test_specific_class_with_name_suffix(self, sorted_classes):
        """具体职业后紧跟名字：剩余部分归入玩家名。"""
        r = parse("酋长张三", sorted_classes)
        assert r["class_"] == "战士"
        assert r["player_name"] == "张三"

    def test_bare_specific_faith_word(self, sorted_classes):
        """正文里单独出现的具体信仰词也要利用（此前只从名字里剔除就丢掉）。"""
        r = parse("繁荣 战士 王五", sorted_classes)
        assert r["specific_faith"] == "繁荣"
        assert r["faith"] == "生命"
        assert r["class_"] == "战士"
        assert r["player_name"] == "王五"

    def test_bare_path_word(self, sorted_classes):
        """命途词只推出命途，不算具体信仰。"""
        r = parse("生命 战士 王五", sorted_classes)
        assert r["faith"] == "生命"
        assert r["specific_faith"] is None
        assert r["class_"] == "战士"
        assert r["player_name"] == "王五"

    def test_tag_wins_over_body_word(self, sorted_classes):
        """标签与正文都给出信仰时，以标签为准（正文不再覆盖）。"""
        r = parse("【欺诈】记忆 法师 李四", sorted_classes)
        assert r["specific_faith"] == "欺诈"

    def test_digits_ignored_in_name(self, sorted_classes):
        r = parse("张三 1218", sorted_classes)
        assert r["player_name"] == "张三"

    def test_multiple_name_words_concatenated(self, sorted_classes):
        r = parse("张 三 战士", sorted_classes)
        assert r["player_name"] == "张三"
        assert r["class_"] == "战士"

    def test_all_faiths_from_tag(self, sorted_classes):
        """16 个具体信仰都能从标签解析出，并映射到合法命途。"""
        from astrbot_plugin_faith_ladder.models import FAITH_TO_PATH, VALID_FAITHS, VALID_PATHS

        for faith in VALID_FAITHS:
            r = parse(f"【{faith}】战士 阿猫", sorted_classes)
            assert r["specific_faith"] == faith, faith
            assert r["faith"] == FAITH_TO_PATH[faith], faith
            assert r["faith"] in VALID_PATHS, faith

    def test_path_in_tag_only_sets_faith(self, sorted_classes):
        """命途写在标签里也要认：同一个词写在正文里本就能识别。"""
        r = parse("【生命】战士 张三", sorted_classes)
        assert r["faith"] == "生命"
        assert r["specific_faith"] is None
        assert r["class_"] == "战士"
        assert r["player_name"] == "张三"

    def test_tag_not_at_start(self, sorted_classes):
        """标签不在开头（如加了前缀）时也要认，并把它从玩家名里摘掉。"""
        r = parse("Lv.9【生命】战士 张三", sorted_classes)
        assert r["faith"] == "生命"
        assert r["class_"] == "战士"
        assert "【" not in (r["player_name"] or "")
        assert "生命" not in (r["player_name"] or "")

    def test_real_card_chaos_hunter(self, sorted_classes):
        """报障名片：混乱·猎人的具体职业叫「渔夫」，它不能抢走玩家名。"""
        r = parse("【混乱】子琅 渔夫 1000 100", sorted_classes)
        assert r["specific_faith"] == "混乱"
        assert r["faith"] == "混沌"
        assert r["class_"] == "猎人"
        assert r["player_name"] == "子琅"


class TestParsingAmbiguities:
    """把已知歧义的现状钉死：这些是**有意保留**的行为，不是待修的 bug。

    历次计划都把它们当成"顺手能修"的缺陷，改一次就要重新论证一遍。这里连同
    card_utils 里的注释一起固定下来——要改先改这里，并确认连写名片的解析不退化。
    """

    def test_specific_class_prefix_truncates_name(self, sorted_classes):
        """名字以具体职业名开头时会被截断：「小丑鱼」→ 职业=小丑 + 名字「鱼」。

        不拆的话，连写型名片「酋长张三」「魔术师1218」就解析不出职业。
        """
        r = parse("【欺诈】小丑鱼 1000 100", sorted_classes)
        assert r["class_"] == "牧师"
        assert r["specific_faith"] == "欺诈"
        assert r["player_name"] == "鱼"

    def test_specific_class_prefix_keeps_longer_remainder(self, sorted_classes):
        """剩余超过一个字时同样归入名字：「小丑鱼丸」→「鱼丸」。"""
        r = parse("【欺诈】小丑鱼丸 1000 100", sorted_classes)
        assert r["class_"] == "牧师"
        assert r["player_name"] == "鱼丸"

    def test_digit_remainder_not_in_name(self, sorted_classes):
        """剩余是数字时（广告昵称）不进玩家名——这条与上一条是同源规则的两种走向。"""
        r = parse("【欺诈】小丑1218", sorted_classes)
        assert r["class_"] == "牧师"
        assert r["player_name"] is None

    def test_name_words_joined_without_separator(self, sorted_classes):
        """多个非关键词直接拼接：被空格切开的名字能还原，独立词也会粘在一起。"""
        assert parse("张 三 战士", sorted_classes)["player_name"] == "张三"
        assert parse("张三 小明 阿伟", sorted_classes)["player_name"] == "张三小明阿伟"


class TestExtractSpecificFaith:
    def test_from_tag(self):
        assert card_utils.extract_specific_faith("【欺诈】法师 李四") == "欺诈"

    def test_path_is_not_specific_faith(self):
        """命途不是具体信仰，应返回 None。"""
        assert card_utils.extract_specific_faith("【生命】战士 张三") is None

    def test_no_tag(self):
        assert card_utils.extract_specific_faith("张三 战士") is None

    def test_empty(self):
        assert card_utils.extract_specific_faith("") is None


class TestExtractCardWords:
    def test_strips_leading_tag(self):
        assert card_utils.extract_card_words("【繁荣】战士 张三") == ["战士", "张三"]

    def test_drops_pure_digits(self):
        assert card_utils.extract_card_words("张三 1218 战士") == ["张三", "战士"]

    def test_no_tag(self):
        assert card_utils.extract_card_words("张三 战士") == ["张三", "战士"]
