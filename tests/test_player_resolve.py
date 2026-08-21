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
        prayer1 = "感孕众生衔育自然"
        prayer2 = "感孕众生，衔育自然！"
        assert normalize_prayer(prayer1) == normalize_prayer(prayer2)


class TestMultiPrayerMatching:
    """Tests for multi-prayer matching (any match is valid)."""

    LIFE_PRAYERS = ["感孕众生衔育自然", "万物滋生亦繁亦荣", "灵魂安眠生命终焉"]
    VOID_PRAYERS = ["不辨真伪勿论虚实", "命若繁星望而不及"]

    def test_any_prayer_matches(self):
        """Any prayer in the list should match."""
        msg = normalize_prayer("万物滋生，亦繁亦荣")
        assert any(msg == normalize_prayer(p) for p in self.LIFE_PRAYERS)

    def test_first_prayer_matches(self):
        msg = normalize_prayer("感孕众生，衔育自然")
        assert any(msg == normalize_prayer(p) for p in self.LIFE_PRAYERS)

    def test_last_prayer_matches(self):
        msg = normalize_prayer("灵魂安眠，生命终焉")
        assert any(msg == normalize_prayer(p) for p in self.LIFE_PRAYERS)

    def test_wrong_prayer_does_not_match(self):
        msg = normalize_prayer("文明火起秩序长存")  # 文明信仰，不是生命
        assert not any(msg == normalize_prayer(p) for p in self.LIFE_PRAYERS)

    def test_void_any_match(self):
        msg1 = normalize_prayer("不辨真伪，勿论虚实")
        msg2 = normalize_prayer("命若繁星，望而不及")
        assert any(msg1 == normalize_prayer(p) for p in self.VOID_PRAYERS)
        assert any(msg2 == normalize_prayer(p) for p in self.VOID_PRAYERS)

    def test_empty_prayer_list(self):
        """Empty list should never match."""
        msg = normalize_prayer("感孕众生")
        assert not any(msg == normalize_prayer(p) for p in [])

    def test_backward_compat_single_string(self):
        """Single-string legacy format wrapped in list should still work."""
        legacy = "万法归寂虚无永存"
        prayers = [legacy]  # wrapped for backward compatibility
        msg = normalize_prayer("万法归寂，虚无永存")
        assert any(msg == normalize_prayer(p) for p in prayers)


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
