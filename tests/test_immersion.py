"""
沉浸感改造（批次 1）：神明短句、试炼刻痕、祷词大成功与连续天数、状态来源文案。

原则：数字部分（`+16 → 1016`）保持原样，叙事只是"多一行"，且都能在配置里关掉或替换。
"""

import pytest

from astrbot_plugin_faith_ladder import progress
from astrbot_plugin_faith_ladder.faith_messages import GENERIC_SCORE_FLAVOR, SCORE_FLAVOR
from astrbot_plugin_faith_ladder.ladder_service import LadderService
from astrbot_plugin_faith_ladder.message_formatter import (
    format_prayer_trigger,
    format_score_result,
    pick_score_flavor,
    render_milestones,
)
from astrbot_plugin_faith_ladder.models import Player
from astrbot_plugin_faith_ladder.prayer_messages import (
    PRAYER_CRIT_MESSAGES,
    PRAYER_MESSAGES,
    pick_prayer_streak_line,
)


def _mk_player(pid: str, ladder_score: int, name: str = "玩家"):
    from astrbot_plugin_faith_ladder.models import Player

    return Player(player_id=pid, group_id="g1", player_name=name, ladder_score=ladder_score)


def _player(faith=None, specific_faith=None):
    return Player(player_id="u1", group_id="g1", player_name="张三",
                  faith=faith, specific_faith=specific_faith)


class TestDetectMilestones:
    def test_crossing_a_hundred(self):
        assert progress.detect_milestones(105, 216) == [(progress.KIND_HUNDRED, 2)]

    def test_crossing_a_thousand(self):
        events = progress.detect_milestones(980, 1010)
        assert (progress.KIND_THOUSAND, 1) in events
        assert (progress.KIND_HUNDRED, 10) in events

    def test_threshold_only_the_first_time(self):
        assert (progress.KIND_THRESHOLD, 1100) in progress.detect_milestones(1000, 1150, 1100)
        second = progress.detect_milestones(1150, 1200, 1100)
        assert all(kind != progress.KIND_THRESHOLD for kind, _ in second), "门槛只该刻一次"

    def test_threshold_exact_boundary_counts(self):
        assert (progress.KIND_THRESHOLD, 1100) in progress.detect_milestones(1099, 1100, 1100)

    def test_no_threshold_when_disabled(self):
        events = progress.detect_milestones(990, 1200, 0)
        assert (progress.KIND_HUNDRED, 12) in events
        assert (progress.KIND_THOUSAND, 1) in events
        assert all(kind != progress.KIND_THRESHOLD for kind, _ in events)

    def test_losing_points_never_creates_milestones(self):
        assert progress.detect_milestones(1200, 900, 1100) == []

    def test_rank_rise(self):
        events = progress.detect_milestones(1000, 1000, rank_before=5, rank_after=3)
        assert events == [(progress.KIND_RANK, 2)]

    def test_rank_fall_is_not_a_milestone(self):
        assert progress.detect_milestones(1000, 1000, rank_before=3, rank_after=5) == []

    def test_render_milestones(self):
        lines = render_milestones([(progress.KIND_HUNDRED, 12), (progress.KIND_THRESHOLD, 1100)])
        assert "—— 第 12 道刻痕。" in lines
        assert any("棋盘" in line for line in lines)


class TestScoreFlavor:
    def test_builtin_pool_by_specific_faith(self):
        player = _player(faith="生命", specific_faith="繁荣")
        for _ in range(10):
            assert pick_score_flavor(player, {}) in SCORE_FLAVOR["繁荣"]

    def test_generic_pool_when_only_path_is_known(self):
        """内置短句池只按 16 个具体信仰分池，只有命途时落到通用池（与祷词/弃誓文案的约定一致）。"""
        player = _player(faith="生命")
        assert pick_score_flavor(player, {}) in GENERIC_SCORE_FLAVOR

    def test_generic_pool_when_no_faith_at_all(self):
        assert pick_score_flavor(_player(), {}) in GENERIC_SCORE_FLAVOR

    def test_config_pool_replaces_builtin(self):
        player = _player(faith="生命", specific_faith="繁荣")
        flavor = pick_score_flavor(player, {"score_flavor_messages": ["只有这一句"]})
        assert flavor == "只有这一句"

    def test_per_faith_config_wins_over_generic(self):
        player = _player(faith="生命", specific_faith="繁荣")
        config = {
            "score_flavor_messages": ["通用句"],
            "score_flavor_messages_繁荣": ["繁荣专属句"],
        }
        assert pick_score_flavor(player, config) == "繁荣专属句"

    def test_empty_config_pool_falls_back(self):
        player = _player(faith="生命", specific_faith="繁荣")
        assert pick_score_flavor(player, {"score_flavor_messages": []}) in SCORE_FLAVOR["繁荣"]


class TestScoreResultWithFlavor:
    def test_numbers_stay_in_arrow_style(self):
        text = format_score_result("Alice", 16, -2, 1016, 98)
        assert "登神之路: +16 → 1016" in text
        assert "觐见之梯: -2 → 98" in text

    def test_flavor_goes_above_the_numbers(self):
        text = format_score_result("Alice", 16, -2, 1016, 98, flavor="【秩序】把你向上推了一寸。")
        assert text.splitlines()[0] == "【秩序】把你向上推了一寸。"
        assert "登神之路: +16 → 1016" in text

    def test_milestones_go_below_the_numbers(self):
        text = format_score_result("Alice", 200, 0, 1200, 100,
                                   milestones=["—— 第 12 道刻痕。"])
        assert text.rstrip().endswith("—— 第 12 道刻痕。")


class TestPrayerCrit:
    def test_crit_uses_its_own_pool(self):
        for _ in range(10):
            msg = format_prayer_trigger("Alice", "欺诈", "欺诈", 2, None, crit=True, streak=0)
            assert any(line.format(god="欺诈", delta=2) in msg for line in PRAYER_CRIT_MESSAGES["欺诈"])

    def test_without_crit_uses_positive_pool(self):
        msg = format_prayer_trigger("Alice", "欺诈", "欺诈", 2, None, crit=False)
        assert any(line.format(god="欺诈", delta=2) in msg for line in PRAYER_MESSAGES["欺诈"]["positive"])

    def test_crit_falls_back_to_positive_when_pool_missing(self):
        # 造一个没有 crit 池的信仰：回落到 positive，不应报错
        import astrbot_plugin_faith_ladder.message_formatter as mf

        original = PRAYER_CRIT_MESSAGES.pop("欺诈")
        try:
            msg = mf.format_prayer_trigger("Alice", "欺诈", "欺诈", 2, None, crit=True)
            assert any(line.format(god="欺诈", delta=2) in msg for line in PRAYER_MESSAGES["欺诈"]["positive"])
        finally:
            PRAYER_CRIT_MESSAGES["欺诈"] = original

    def test_config_crit_pool_wins(self):
        config = {"prayer_trigger_messages_crit": ["只有这一句 {delta}"]}
        msg = format_prayer_trigger("Alice", "欺诈", "欺诈", 2, config, crit=True)
        assert "只有这一句 2" in msg

    def test_crit_flag_ignored_on_mismatch(self):
        """渎神时即使分数落在上限，也不该走大成功文案。"""
        msg = format_prayer_trigger("Alice", "欺诈", "秩序", 2, None, crit=True)
        assert "看到了你对" in msg


class TestPrayerStreak:
    def test_no_line_before_two_days(self):
        assert pick_prayer_streak_line(0) == ""
        assert pick_prayer_streak_line(1) == ""

    def test_thresholds(self):
        assert "连续第 3 天" in pick_prayer_streak_line(3)
        assert "开始记得你" in pick_prayer_streak_line(7)
        assert "不只是一个过客" in pick_prayer_streak_line(40)

    def test_line_appended_to_reply(self):
        msg = format_prayer_trigger("Alice", "欺诈", "欺诈", 2, None, streak=3)
        assert "连续第 3 天" in msg

    def test_no_streak_line_on_mismatch(self):
        msg = format_prayer_trigger("Alice", "欺诈", "秩序", -2, None, streak=9)
        assert "连续" not in msg


class TestPrayerStreakDb:
    async def test_zero_without_records(self, db_manager):
        await db_manager.upsert_player("g1", "u1", "Alice")
        assert await db_manager.get_prayer_streak("g1", "u1") == 0

    async def test_today_only(self, db_manager):
        await db_manager.upsert_player("g1", "u1", "Alice")
        await db_manager.record_prayer_hit("g1", "u1", 1)
        assert await db_manager.get_prayer_streak("g1", "u1") == 1

    async def test_consecutive_days(self, db_manager):
        from datetime import datetime, timedelta

        from astrbot_plugin_faith_ladder.db_manager import BEIJING_TZ

        await db_manager.upsert_player("g1", "u1", "Alice")
        today = datetime.now(BEIJING_TZ).date()
        for offset in (0, 1, 2):
            day = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
            await db_manager._db.execute(
                "INSERT INTO prayer_daily_hits (group_id, player_id, hit_date, delta) VALUES (?, ?, ?, ?)",
                ("g1", "u1", day, 1),
            )
        await db_manager.commit()
        assert await db_manager.get_prayer_streak("g1", "u1") == 3

    async def test_gap_stops_the_streak(self, db_manager):
        from datetime import datetime, timedelta

        from astrbot_plugin_faith_ladder.db_manager import BEIJING_TZ

        await db_manager.upsert_player("g1", "u1", "Alice")
        today = datetime.now(BEIJING_TZ).date()
        for offset in (0, 3, 4):  # 昨天漏了一天
            day = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
            await db_manager._db.execute(
                "INSERT INTO prayer_daily_hits (group_id, player_id, hit_date, delta) VALUES (?, ?, ?, ?)",
                ("g1", "u1", day, 1),
            )
        await db_manager.commit()
        assert await db_manager.get_prayer_streak("g1", "u1") == 1

    async def test_players_and_groups_are_isolated(self, db_manager):
        from datetime import datetime

        from astrbot_plugin_faith_ladder.db_manager import BEIJING_TZ

        await db_manager.upsert_player("g1", "u1", "Alice")
        await db_manager.upsert_player("g1", "u2", "Bob")
        today = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
        await db_manager._db.execute(
            "INSERT INTO prayer_daily_hits (group_id, player_id, hit_date, delta) VALUES (?, ?, ?, ?)",
            ("g1", "u2", today, 1),
        )
        await db_manager.commit()
        assert await db_manager.get_prayer_streak("g1", "u1") == 0
        assert await db_manager.get_prayer_streak("g1", "u2") == 1


class TestAddScoreImmersion:
    """跑真实服务层：短句、刻痕、以及开关能关掉。"""

    async def _service(self, db, **config):
        return LadderService(db, config_getter=lambda: dict(config))

    async def test_flavor_line_comes_from_the_players_faith(self, db_manager):
        await db_manager.upsert_player("g1", "u1", "Alice")
        await db_manager.set_player_specific_faith("g1", "u1", "繁荣")
        await db_manager.commit()

        service = LadderService(db_manager)
        ok, msg = await service.add_score("g1", "u1", "Alice", 30, 0, "admin")
        assert ok is True
        assert msg.splitlines()[0] in SCORE_FLAVOR["繁荣"]
        assert "登神之路: +30 → 1030" in msg

    async def test_switch_off_restores_plain_reply(self, db_manager):
        await db_manager.upsert_player("g1", "u1", "Alice")
        service = await self._service(db_manager, score_flavor_enabled=False)
        ok, msg = await service.add_score("g1", "u1", "Alice", 30, 0, "admin")
        assert ok is True
        assert msg.splitlines()[0] == "Alice 的积分已更新"

    async def test_threshold_milestone_on_first_crossing(self, db_manager):
        await db_manager.upsert_player("g1", "u1", "Alice")  # 1000 分，门槛默认 1100
        service = LadderService(db_manager)
        ok, msg = await service.add_score("g1", "u1", "Alice", 150, 0, "admin")
        assert ok is True
        assert "棋盘" in msg

    async def test_hundred_milestone(self, db_manager):
        await db_manager.upsert_player("g1", "u1", "Alice")
        service = await self._service(db_manager, score_flavor_enabled=False)
        ok, msg = await service.add_score("g1", "u1", "Alice", 250, 0, "admin")
        assert "—— 第 12 道刻痕。" in msg
        assert "—— 阶梯轰鸣" not in msg, "1000→1250 并没有跨过千位"

    async def test_thousand_milestone(self, db_manager):
        await db_manager.upsert_player("g1", "u1", "Alice")
        await db_manager.update_scores("g1", "u1", -100, 0, "admin")  # 降到 900
        service = await self._service(db_manager, score_flavor_enabled=False)
        ok, msg = await service.add_score("g1", "u1", "Alice", 150, 0, "admin")
        assert ok is True
        assert "—— 阶梯轰鸣：你已走过第 1 个千阶。" in msg

    async def test_rank_milestone(self, db_manager):
        await db_manager.upsert_player("g1", "u1", "Alice")
        await db_manager.upsert_player("g1", "u2", "Bob")
        await db_manager.update_scores("g1", "u2", 500, 0, "admin")  # Bob 1500
        service = await self._service(db_manager, score_flavor_enabled=False)
        ok, msg = await service.add_score("g1", "u1", "Alice", 600, 0, "admin")
        assert ok is True
        assert "—— 你越过了 1 个人。" in msg


class TestStatusSourceText:
    async def test_faith_named_status_credits_that_god(self, db_manager):
        await db_manager.upsert_player("g1", "u1", "Alice")
        service = LadderService(db_manager)
        ok, msg = await service.add_status("g1", "Alice", "沉默", 3)
        assert ok is True
        assert "由【沉默】降下" in msg
        assert "3天" in msg

    async def test_other_status_is_generic(self, db_manager):
        await db_manager.upsert_player("g1", "u1", "Alice")
        service = LadderService(db_manager)
        ok, msg = await service.add_status("g1", "Alice", "虚弱", 2)
        assert ok is True
        assert "由神明的意志降下" in msg

    async def test_blocked_actions_still_reported(self, db_manager):
        await db_manager.upsert_player("g1", "u1", "Alice")
        service = LadderService(db_manager)
        ok, msg = await service.add_status("g1", "Alice", "沉默", 2, "prayer")
        assert ok is True
        assert "禁止：祷词" in msg


class TestTierMarks:
    """位阶徽记：纯符号（不出现中文阶名），阈值与符号都可配置。"""

    def test_default_mark_ladder(self):
        assert progress.tier_mark(0) == "☽"
        assert progress.tier_mark(999) == "☽"
        assert progress.tier_mark(1000) == "☿"
        assert progress.tier_mark(1100) == "♀"
        assert progress.tier_mark(5000) == "☉"
        assert progress.tier_mark(99999) == "☉"

    def test_below_first_threshold_is_lowest_tier(self):
        assert progress.tier_index(0, [100, 200]) == 0
        assert progress.tier_index(50, [100, 200]) == 0

    def test_custom_marks_follow_thresholds(self):
        marks = ["A", "B", "C"]
        assert progress.tier_mark(0, [0, 10, 20], marks) == "A"
        assert progress.tier_mark(10, [0, 10, 20], marks) == "B"
        assert progress.tier_mark(20, [0, 10, 20], marks) == "C"

    def test_fewer_marks_than_tiers_reuses_last(self):
        """少配几个徽记不该整个不显示——超出的阶层沿用最后一个符号。"""
        assert progress.tier_mark(5000, None, ["A", "B"]) == "B"

    def test_bad_config_falls_back_to_defaults(self):
        assert progress.tier_mark(1000, ["abc"], []) == "☿"
        assert progress._clean_thresholds([100, "x", 50]) == [50, 100]

    def test_build_tier_marks_maps_by_player_id(self):
        low = _mk_player("u1", 1000)
        high = _mk_player("u2", 6000)
        marks = progress.build_tier_marks([low, high])
        assert marks == {"u1": "☿", "u2": "☉"}

    def test_marks_appear_on_leaderboard(self):
        from astrbot_plugin_faith_ladder.message_formatter import format_leaderboard

        players = [_mk_player("u1", 1200, "Bob"), _mk_player("u2", 1100, "Alice")]
        text = format_leaderboard(players, 10, tier_marks={"u1": "C", "u2": "C"})
        assert "1. C Bob" in text
        assert "2. C Alice" in text

    def test_leaderboard_without_marks_unchanged(self):
        from astrbot_plugin_faith_ladder.message_formatter import format_leaderboard

        players = [_mk_player("u1", 1200, "Bob")]
        assert "1. Bob" in format_leaderboard(players, 10)

    def test_player_card_shows_mark(self):
        from astrbot_plugin_faith_ladder.message_formatter import format_player_card

        card = format_player_card(_mk_player("u1", 1200, "张三"), tier_marks={"u1": "☉"})
        assert "姓名: ☉ 张三" in card

    def test_player_card_without_marks_unchanged(self):
        from astrbot_plugin_faith_ladder.message_formatter import format_player_card

        card = format_player_card(_mk_player("u1", 1200, "张三"))
        assert "姓名: 张三" in card


class TestTierMarksThroughService:
    """走服务层：徽记来自配置，关掉（空表）也不会报错。"""

    async def test_leaderboard_uses_configured_marks(self, db_manager):
        await db_manager.upsert_player("g1", "u1", "Alice")
        await db_manager.update_scores("g1", "u1", 200, 0, "admin")  # 1200
        service = LadderService(db_manager, config_getter=lambda: {
            "tier_marks": ["A", "B", "C", "D", "E", "F", "G", "H", "I"],
        })
        text = await service.get_leaderboard_text("g1", 10, 0)
        assert "1. C Alice" in text

    async def test_player_card_uses_configured_marks(self, db_manager):
        await db_manager.upsert_player("g1", "u1", "Alice")
        await db_manager.update_scores("g1", "u1", 200, 0, "admin")
        service = LadderService(db_manager, config_getter=lambda: {
            "tier_marks": ["A", "B", "C", "D", "E", "F", "G", "H", "I"],
        })
        text, _ = await service.get_player_cards_by_names("g1", ["Alice"])
        assert "姓名: C Alice" in text

    async def test_default_symbols_are_used_without_config(self, db_manager):
        """没有配置读取器（如单测直接构造）时用内置符号表，不显示中文阶名。"""
        await db_manager.upsert_player("g1", "u1", "Alice")
        service = LadderService(db_manager)
        text = await service.get_leaderboard_text("g1", 10, 0)
        assert any(mark in text for mark in progress.DEFAULT_TIER_MARKS)
        assert "位阶" not in text and "阶" not in text
