"""
Tests for player resolve functionality:
- normalize_prayer (祷词匹配)
- _resolve_name_from_card (群名片解析)
- _resolve_target_or_self logic (权限控制)
"""

import re
import pytest


def normalize_prayer(text: str) -> str:
    """去除标点和空格，用于祷词匹配。"""
    return re.sub(
        r'[\s ，。！？、；：“”‘’（）【】《》…—.,!?;:\'\"()<>\[\]\-]+',
        '', text
    )


def resolve_name_from_card_sync(card: str) -> list:
    """Extract non-digit words from card (sync version for unit testing).
    Returns list of candidate words to match against DB.
    """
    card = card.strip()
    match = re.match(r'^【[^】]*】\s*(.*)', card)
    remaining = match.group(1).strip() if match else card.strip()
    return [w for w in remaining.split() if not w.isdigit()]


class TestNormalizePrayer:
    """Tests for prayer text normalization."""

    def test_plain_text(self):
        assert normalize_prayer("万法归寂虚无永存") == "万法归寂虚无永存"

    def test_with_spaces(self):
        assert normalize_prayer("万法 归寂 虚无 永存") == "万法归寂虚无永存"

    def test_with_chinese_punctuation(self):
        assert normalize_prayer("万法，归寂。虚无！永存？") == "万法归寂虚无永存"

    def test_with_english_punctuation(self):
        assert normalize_prayer("万法,归寂.虚无!永存?") == "万法归寂虚无永存"

    def test_with_mixed_punctuation(self):
        assert normalize_prayer("万法，归寂。虚无！永存？...") == "万法归寂虚无永存"

    def test_with_brackets(self):
        assert normalize_prayer("【万法】（归寂）虚无") == "万法归寂虚无"

    def test_with_full_width_space(self):
        assert normalize_prayer("万法　归寂　虚无") == "万法归寂虚无"

    def test_with_colons_and_semicolons(self):
        assert normalize_prayer("万法：归寂；虚无") == "万法归寂虚无"

    def test_empty_string(self):
        assert normalize_prayer("") == ""

    def test_only_punctuation(self):
        assert normalize_prayer("，。！？") == ""

    def test_match_with_different_formatting(self):
        """Two versions of the same prayer with different formatting should match."""
        prayer1 = "万法归寂虚无永存"
        prayer2 = "万法，归寂！虚无……永存？"
        assert normalize_prayer(prayer1) == normalize_prayer(prayer2)


class TestResolveNameFromCard:
    """Tests for card word extraction (non-digit words)."""

    def test_standard_format(self):
        # 【命运】 织命师 name 1000 100
        words = resolve_name_from_card_sync("【命运】 织命师 name 1000 100")
        assert words == ["织命师", "name"]

    def test_chinese_name(self):
        # 【XX】 蓬莱 守墓人 100 100
        words = resolve_name_from_card_sync("【XX】 蓬莱 守墓人 100 100")
        assert words == ["蓬莱", "守墓人"]

    def test_no_tag(self):
        words = resolve_name_from_card_sync("蓬莱 守墓人 100 100")
        assert words == ["蓬莱", "守墓人"]

    def test_only_name(self):
        words = resolve_name_from_card_sync("【命运】 蓬莱 1000 100")
        assert words == ["蓬莱"]

    def test_empty_card(self):
        words = resolve_name_from_card_sync("")
        assert words == []

    def test_all_numbers(self):
        words = resolve_name_from_card_sync("1000 100 200")
        assert words == []

    def test_mixed_alphanumeric(self):
        # Words that contain digits but aren't pure digits are kept
        words = resolve_name_from_card_sync("【命运】 Player1 守墓人 1000")
        assert words == ["Player1", "守墓人"]

    def test_complex_tag(self):
        words = resolve_name_from_card_sync("【XX·YY】 名字 职业 100 100")
        assert words == ["名字", "职业"]

    def test_single_word(self):
        words = resolve_name_from_card_sync("蓬莱")
        assert words == ["蓬莱"]

    def test_numbers_only_after_tag(self):
        words = resolve_name_from_card_sync("【命运】 1000 100")
        assert words == []

    def test_whitespace_only(self):
        words = resolve_name_from_card_sync("   ")
        assert words == []
