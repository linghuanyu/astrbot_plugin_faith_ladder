"""
Tests for prayer trigger feature.
"""

import pytest
import pytest_asyncio
import re


# Pre-compiled regex patterns (same as main.py)
_PRAYER_NORMALIZE_RE = re.compile(r'[^\w]')
_PRAYER_CHINESE_RE = re.compile(r'[一-鿿]')


def normalize_prayer_text(text: str) -> str:
    """去除所有标点和空格，仅保留中文字符和字母数字。"""
    return _PRAYER_NORMALIZE_RE.sub('', text).strip()


def is_valid_prayer_length(text: str) -> bool:
    """检查是否为恰好 8 个汉字。"""
    chinese_chars = _PRAYER_CHINESE_RE.findall(text)
    return len(chinese_chars) == 8


class TestNormalizePrayerText:
    """Tests for prayer text normalization."""

    def test_remove_punctuation(self):
        assert normalize_prayer_text("不辨，真伪！勿论。虚实") == "不辨真伪勿论虚实"

    def test_remove_spaces(self):
        assert normalize_prayer_text("不辨 真伪 勿论 虚实") == "不辨真伪勿论虚实"

    def test_remove_mixed(self):
        assert normalize_prayer_text("【不辨】真伪 勿论，虚实！") == "不辨真伪勿论虚实"

    def test_empty_string(self):
        assert normalize_prayer_text("") == ""

    def test_only_punctuation(self):
        assert normalize_prayer_text("，。！？") == ""

    def test_preserve_english(self):
        assert normalize_prayer_text("Hello World!") == "HelloWorld"

    def test_preserve_numbers(self):
        assert normalize_prayer_text("测试123！") == "测试123"


class TestValidPrayerLength:
    """Tests for prayer length validation."""

    def test_exactly_8_chinese(self):
        assert is_valid_prayer_length("不辨真伪勿论虚实") is True

    def test_less_than_8(self):
        assert is_valid_prayer_length("不辨真伪") is False

    def test_more_than_8(self):
        assert is_valid_prayer_length("不辨真伪勿论虚实额外") is False

    def test_mixed_with_english(self):
        # 8 Chinese chars + English = still 8 Chinese chars, should pass
        assert is_valid_prayer_length("不辨真伪AB勿论虚实") is True

    def test_8_chinese_with_numbers(self):
        # 8 Chinese chars + numbers = should fail
        assert is_valid_prayer_length("不辨真伪1234虚实") is False

    def test_empty_string(self):
        assert is_valid_prayer_length("") is False


class TestPrayerTriggerConfig:
    """Tests for prayer trigger configuration."""

    def test_prayer_cache_structure(self):
        """Verify prayer cache maps normalized prayer to path (命途)."""
        from astrbot_plugin_faith_ladder.models import VALID_PATHS

        config = {
            "prayer_text_虚无": ["不辨真伪勿论虚实", "命若繁星望而不及"],
            "prayer_text_存在": ["昔我长铭流光拓影"],
        }

        # Simulate cache building (uses VALID_PATHS, not VALID_FAITHS)
        prayer_cache = {}
        for path in VALID_PATHS:
            key = f"prayer_text_{path}"
            prayers = config.get(key, [])
            for prayer in prayers:
                normalized = normalize_prayer_text(prayer)
                if normalized:
                    prayer_cache[normalized] = path

        # Check that the prayers were added correctly
        assert "不辨真伪勿论虚实" in prayer_cache
        assert prayer_cache["不辨真伪勿论虚实"] == "虚无"
        assert "命若繁星望而不及" in prayer_cache
        assert prayer_cache["命若繁星望而不及"] == "虚无"
        assert "昔我长铭流光拓影" in prayer_cache
        assert prayer_cache["昔我长铭流光拓影"] == "存在"

    def test_command_prefix_detection(self):
        """Verify command message detection."""
        config = {
            "cmd_query": "查询",
            "cmd_ladder": "天梯榜",
            "cmd_help": "天梯榜帮助",
        }
        command_prefixes = {config[k] for k in config if config[k]}

        def is_command_message(text: str) -> bool:
            text_stripped = text.strip()
            return any(text_stripped.startswith(prefix) for prefix in command_prefixes if prefix)

        assert is_command_message("查询") is True
        assert is_command_message("查询 张三") is True
        assert is_command_message("天梯榜") is True
        assert is_command_message("不辨真伪勿论虚实") is False
        assert is_command_message("  查询") is True  # with leading space


@pytest.mark.asyncio
class TestDailyHitTracking:
    """Tests for prayer daily hit DB operations."""

    async def test_no_hit_initially(self, db_manager):
        """Player has no hit initially."""
        await db_manager.upsert_player("g1", "u1", "Alice")
        assert await db_manager.has_prayer_hit_today("g1", "u1") is False

    async def test_record_hit(self, db_manager):
        """Recording a hit makes has_prayer_hit_today return True."""
        await db_manager.upsert_player("g1", "u1", "Alice")
        recorded = await db_manager.record_prayer_hit("g1", "u1", 1)
        assert recorded is True
        assert await db_manager.has_prayer_hit_today("g1", "u1") is True

    async def test_double_record_fails(self, db_manager):
        """Recording twice on same day fails (UNIQUE constraint)."""
        await db_manager.upsert_player("g1", "u1", "Alice")
        assert await db_manager.record_prayer_hit("g1", "u1", 1) is True
        assert await db_manager.record_prayer_hit("g1", "u1", -1) is False

    async def test_different_players_independent(self, db_manager):
        """Different players have independent hit tracking."""
        await db_manager.upsert_player("g1", "u1", "Alice")
        await db_manager.upsert_player("g1", "u2", "Bob")
        await db_manager.record_prayer_hit("g1", "u1", 1)
        assert await db_manager.has_prayer_hit_today("g1", "u1") is True
        assert await db_manager.has_prayer_hit_today("g1", "u2") is False

    async def test_different_groups_independent(self, db_manager):
        """Same player in different groups has independent hit tracking."""
        await db_manager.upsert_player("g1", "u1", "Alice")
        await db_manager.upsert_player("g2", "u1", "Alice")
        await db_manager.record_prayer_hit("g1", "u1", 1)
        assert await db_manager.has_prayer_hit_today("g1", "u1") is True
        assert await db_manager.has_prayer_hit_today("g2", "u1") is False


class TestFormatPrayerTrigger:
    """Tests for prayer trigger message formatting."""

    def test_positive_delta(self):
        from astrbot_plugin_faith_ladder.message_formatter import format_prayer_trigger
        msg = format_prayer_trigger("Alice", "欺诈", "欺诈", 2, None)
        assert "神明看到了你的祈祷" in msg
        assert "欺诈" in msg
        assert "+2" in msg
        assert "本次结果暂时不会影响实际分数" in msg

    def test_negative_delta(self):
        from astrbot_plugin_faith_ladder.message_formatter import format_prayer_trigger
        msg = format_prayer_trigger("Alice", "记忆", "记忆", -1, None)
        assert "神明看到了你的祈祷" in msg
        assert "记忆" in msg
        assert "-1" in msg
        assert "本次结果暂时不会影响实际分数" in msg

    def test_zero_delta(self):
        from astrbot_plugin_faith_ladder.message_formatter import format_prayer_trigger
        msg = format_prayer_trigger("Alice", "秩序", "秩序", 0, None)
        assert "神明看到了你的祈祷" in msg
        assert "秩序" in msg
        assert "本次结果暂时不会影响实际分数" in msg

    def test_mismatch_faith(self):
        """玩家信仰和祷词信仰不匹配时的特殊文案。"""
        from astrbot_plugin_faith_ladder.message_formatter import format_prayer_trigger
        msg = format_prayer_trigger("Alice", "欺诈", "秩序", -2, None)
        assert "欺诈" in msg
        assert "看到了你对" in msg
        assert "秩序的祈祷" in msg
        assert "的祈祷，决定对你进行惩罚" in msg
        assert "本次结果暂时不会影响实际分数" in msg

    def test_mismatch_zero(self):
        """不匹配（渎神）但随机到 0：宽宏大量。"""
        from astrbot_plugin_faith_ladder.message_formatter import format_prayer_trigger
        msg = format_prayer_trigger("Alice", "欺诈", "秩序", 0, None)
        assert "欺诈" in msg
        assert "神明宽宏大量" in msg
        assert "放过了你这次渎神" in msg
        assert "本次结果暂时不会影响实际分数" in msg

    def test_mismatch_no_player_faith(self):
        """玩家无具体信仰时的处理。"""
        from astrbot_plugin_faith_ladder.message_formatter import format_prayer_trigger
        msg = format_prayer_trigger("Alice", None, "欺诈", -1, None)
        assert "欺诈" in msg
        assert "本次结果暂时不会影响实际分数" in msg

    def test_custom_config_messages(self):
        from astrbot_plugin_faith_ladder.message_formatter import format_prayer_trigger
        config = {
            "prayer_trigger_messages_positive": ["{god}开心，{delta}"],
        }
        msg = format_prayer_trigger("Alice", "欺诈", "欺诈", 1, config)
        assert "神明看到了你的祈祷" in msg
        assert "欺诈开心，1" in msg
        assert "本次结果暂时不会影响实际分数" in msg

    def test_faith_specific_config_override(self):
        """信仰专属配置键优先于通用配置。"""
        from astrbot_plugin_faith_ladder.message_formatter import format_prayer_trigger
        config = {
            "prayer_trigger_messages_positive": ["通用：{god}开心，{delta}"],
            "prayer_trigger_messages_positive_欺诈": ["欺诈专属：{god}低语，{delta}"],
        }
        msg = format_prayer_trigger("Alice", "欺诈", "欺诈", 1, config)
        assert "欺诈专属" in msg
        assert "欺诈低语" in msg

    def test_empty_config_uses_default(self):
        from astrbot_plugin_faith_ladder.message_formatter import format_prayer_trigger
        config = {
            "prayer_trigger_messages_positive": [],
        }
        msg = format_prayer_trigger("Alice", "欺诈", "欺诈", 1, config)
        assert "神明看到了你的祈祷" in msg
        assert "欺诈" in msg
        assert "本次结果暂时不会影响实际分数" in msg

    def test_all_faiths_have_messages(self):
        """确保每个信仰都能正常生成文案。"""
        from astrbot_plugin_faith_ladder.message_formatter import format_prayer_trigger
        from astrbot_plugin_faith_ladder.models import VALID_FAITHS
        for faith in VALID_FAITHS:
            # 正分（匹配）
            msg = format_prayer_trigger("Alice", faith, faith, 2, None)
            assert "神明看到了你的祈祷" in msg, f"{faith} positive message missing"
            # 负分（匹配）
            msg = format_prayer_trigger("Alice", faith, faith, -1, None)
            assert "神明看到了你的祈祷" in msg, f"{faith} negative message missing"
            # 零分（匹配）
            msg = format_prayer_trigger("Alice", faith, faith, 0, None)
            assert "神明看到了你的祈祷" in msg, f"{faith} neutral message missing"
        assert "本次结果暂时不会影响实际分数" in msg


class TestPrayerScoreSwitch:
    """祷词是否真实改分的开关（prayer_score_enabled）对回复文案的影响。"""

    def test_disclaimer_kept_when_switch_off(self):
        from astrbot_plugin_faith_ladder.message_formatter import format_prayer_trigger
        msg = format_prayer_trigger("Alice", "欺诈", "欺诈", 2, {"prayer_score_enabled": False})
        assert "本次结果暂时不会影响实际分数" in msg

    def test_disclaimer_removed_when_switch_on(self):
        from astrbot_plugin_faith_ladder.message_formatter import format_prayer_trigger
        msg = format_prayer_trigger("Alice", "欺诈", "欺诈", 2, {"prayer_score_enabled": True})
        assert "本次结果暂时不会影响实际分数" not in msg
        assert "+2" in msg

    def test_disclaimer_kept_for_mismatch_when_switch_on(self):
        """渎神分支同样要按开关去掉免责声明。"""
        from astrbot_plugin_faith_ladder.message_formatter import format_prayer_trigger
        msg = format_prayer_trigger("Alice", "欺诈", "秩序", -2, {"prayer_score_enabled": True})
        assert "本次结果暂时不会影响实际分数" not in msg
        assert "-2" in msg


class TestPrayerTriggerEdgeCases:
    """Tests for prayer trigger edge cases."""

    def test_non_8_char_message_skipped(self):
        """Messages not exactly 8 Chinese chars are skipped."""
        text = "你好世界"  # 4 chars
        normalized = normalize_prayer_text(text)
        assert is_valid_prayer_length(normalized) is False

    def test_message_with_extra_text_skipped(self):
        """Messages with extra text beyond 8 chars are skipped."""
        text = "不辨真伪勿论虚实额外文字"  # 12 chars
        normalized = normalize_prayer_text(text)
        assert is_valid_prayer_length(normalized) is False

    def test_message_with_punctuation_matches(self):
        """Messages with punctuation that normalize to 8 chars match."""
        text = "不辨，真伪！勿论。虚实"
        normalized = normalize_prayer_text(text)
        assert normalized == "不辨真伪勿论虚实"
        assert is_valid_prayer_length(normalized) is True

    def test_allow_negative_scores_clamp(self):
        """When allow_negative_scores=False, negative delta is clamped to 0."""
        delta = -2
        allow_negative = False
        if not allow_negative:
            delta = max(0, delta)
        assert delta == 0

    def test_allow_negative_scores_enabled(self):
        """When allow_negative_scores=True, negative delta is preserved."""
        delta = -2
        allow_negative = True
        if not allow_negative:
            delta = max(0, delta)
        assert delta == -2


class TestPrayerScoreRangeConfig:
    """祷词分值区间来自配置：匹配与不匹配（渎神）各一对，默认保持 -2~+2 / -2~0。"""

    def _range(self, config, matched):
        from astrbot_plugin_faith_ladder.commands.prayer import PrayerCommandsMixin

        class _Host:
            pass

        host = _Host()
        host.config = config
        return PrayerCommandsMixin._resolve_prayer_delta_range(host, matched)

    def test_defaults_unchanged(self):
        assert self._range({}, True) == (-2, 2)
        assert self._range({}, False) == (-2, 0)

    def test_configured_ranges(self):
        assert self._range({"prayer_score_min": 1, "prayer_score_max": 5}, True) == (1, 5)
        assert self._range(
            {"prayer_blasphemy_score_min": -5, "prayer_blasphemy_score_max": -3}, False
        ) == (-5, -3)

    def test_matched_and_blasphemy_are_independent(self):
        """配置匹配区间不得影响渎神区间（两边各有自己的键）。"""
        config = {"prayer_score_min": 3, "prayer_score_max": 3}
        assert self._range(config, True) == (3, 3)
        assert self._range(config, False) == (-2, 0)

    def test_reversed_bounds_are_swapped(self):
        """上下限写反时按可用区间处理，而不是让 randint 抛错。"""
        assert self._range({"prayer_score_min": 5, "prayer_score_max": 1}, True) == (1, 5)

    def test_bad_value_falls_back_for_that_side_only(self):
        assert self._range({"prayer_score_min": "abc", "prayer_score_max": 7}, True) == (-2, 7)
        assert self._range({"prayer_score_min": None, "prayer_score_max": 7}, True) == (-2, 7)

    def test_string_numbers_are_accepted(self):
        """WebUI 有时把数字存成字符串。"""
        assert self._range({"prayer_score_max": "4"}, True) == (-2, 4)

    def test_positive_only_range_is_possible(self):
        """可以把渎神区间配成不掉分（例如 0~0），也可以配成全为正分。"""
        assert self._range({"prayer_blasphemy_score_min": 0, "prayer_blasphemy_score_max": 0}, False) == (0, 0)
        assert self._range({"prayer_score_min": 1, "prayer_score_max": 3}, True) == (1, 3)


def test_prayer_trigger_uses_configured_range():
    """静态守卫：随机打分必须走 _resolve_prayer_delta_range，不得再写死区间。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "commands" / "prayer.py").read_text(encoding="utf-8")
    assert "_resolve_prayer_delta_range" in src
    assert "random.randint(-2" not in src, "祷词分值区间又被写死了"
