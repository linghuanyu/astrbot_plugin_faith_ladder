"""
祈愿试炼服务层测试。

除常规流程外，这里专门钉两条最容易「改着改着就没人发现」的东西：

1. **星期名额表是按队名日期（= 开团日 + 状态天数）反推的**。默认 `[1,1,0,1,1,0,2]`
   的实际效果是「周三与周日不能开团、周四 2 支、其余 1 支，每周最多 6 次发车」。
   谁要是把口径改成「按开团当天」，这条会立刻红。
2. **名单的两种输出**：不给分数只列名字；给分数必须产出真能被
   `parse_batch_scores` 解出 N 条的文本（裸名单会被它整块丢弃）。
"""

from datetime import datetime, timedelta

import pytest
import pytest_asyncio

from astrbot_plugin_faith_ladder.db_manager import (
    BEIJING_TZ,
    WISH_DEPARTED,
    WISH_DISBANDED,
    WISH_RECRUITING,
    WISH_VOIDED,
    DatabaseManager,
)
from astrbot_plugin_faith_ladder.ladder_service import LadderService
from astrbot_plugin_faith_ladder.wish_service import WEEKDAY_LABELS, WishService

GROUP = "g1"
OTHER_GROUP = "g2"
# 夹具把时钟定在 2026-09-25（周五）：名额日期 = 开团日 + 3 天 = 09-28（周一），
# 于是默认队名正好是 09月28日祈愿试炼。夹具里有一条断言把这个耦合钉住，
# 改日期会让夹具直接报错，而不是让下面一堆测试去操作一支并不存在的队伍。
TEAM_NAME = "09月28日祈愿试炼"


class _Clock:
    """可控时钟：服务层的 now() 指向它，于是日期与名额都可由测试决定。"""

    def __init__(self, when: datetime):
        self.value = when

    def jump_to_weekday(self, label: str) -> datetime:
        target = WEEKDAY_LABELS.index(label)
        while self.value.weekday() != target:
            self.value += timedelta(days=1)
        return self.value


@pytest_asyncio.fixture
async def ctx(temp_data_dir):
    db = DatabaseManager(temp_data_dir)
    await db.initialize()

    friday = datetime(2026, 9, 25, 12, 0, tzinfo=BEIJING_TZ)
    assert friday.weekday() == 4, "夹具基准日必须是周五"
    clock = _Clock(friday)

    config = {"wish_groups": [GROUP]}
    svc = WishService(db, lambda: config)
    svc.now = lambda: clock.value
    assert svc.default_team_name(svc.slot_date_of()) == TEAM_NAME

    for who in ["甲", "乙", "丙", "丁", "戊", "己", "庚", "辛"]:
        await db.upsert_player(GROUP, f"name:{who}", who)
        await db.upsert_player(OTHER_GROUP, f"name:{who}", who)
    yield db, svc, config, clock
    await db.close()


def _pid(who: str) -> str:
    return f"name:{who}"


async def _statuses(db, who, group=GROUP):
    return await db.get_player_statuses(group, _pid(who))


async def _team(db, name=TEAM_NAME, group=GROUP):
    return await db.get_wish_team_by_name(group, name)


async def _backdate(db, team_id, column="last_reminded_at"):
    await db._db.execute(
        f"UPDATE wish_teams SET {column} = '2000-01-01 00:00:00' WHERE id = ?", (team_id,)
    )
    await db._db.commit()


class TestDateAndQuota:
    def test_default_team_name(self, ctx):
        _, svc, _, _ = ctx
        assert svc.default_team_name("2026-09-28") == TEAM_NAME

    def test_slot_date_is_create_day_plus_days(self, ctx):
        _, svc, _, clock = ctx
        assert svc.status_days() == 3
        assert svc.slot_date_of(clock.value) == (
            clock.value + timedelta(days=3)
        ).strftime("%Y-%m-%d")
        assert svc.create_date_of(clock.value) == clock.value.strftime("%Y-%m-%d")

    def test_weekly_table_derivation(self, ctx):
        """反推「开团日是周几 → 能开几支」，把口径钉死。

        取连续 7 天即可覆盖全部星期几，不必先对齐到周一。
        """
        _, svc, _, clock = ctx
        by_weekday = {}
        for offset in range(7):
            day = clock.value + timedelta(days=offset)
            by_weekday[WEEKDAY_LABELS[day.weekday()]] = svc.slot_limit(svc.slot_date_of(day))

        assert by_weekday == {
            "周一": 1, "周二": 1, "周三": 0, "周四": 2, "周五": 1, "周六": 1, "周日": 0,
        }, "星期名额表的口径变了（默认按队名日期算，不是按开团当天）"
        assert sum(by_weekday.values()) == 6, "每周最多 6 次发车"

    def test_slot_date_carries_weekday_label(self, ctx):
        _, svc, _, _ = ctx
        assert svc.format_slot("2026-09-28") == "09月28日（周一）"

    async def test_closed_day_blocks_create(self, ctx):
        _, svc, _, clock = ctx
        clock.jump_to_weekday("周三")

        outcome = await svc.create(GROUP, _pid("甲"), "甲")
        assert (outcome.ok, outcome.code) == (False, "closed_day")
        assert "不安排祈愿试炼" in outcome.reply

    async def test_quota_exhausted_blocks_create(self, ctx):
        db, svc, _, _ = ctx
        await svc.create(GROUP, _pid("甲"), "甲", capacity=2)
        await svc.join(GROUP, _pid("乙"), "乙")  # 发车，用掉唯一名额
        assert await db.slot_usage(GROUP, svc.slot_date_of()) == 1

        outcome = await svc.create(GROUP, _pid("丙"), "丙")
        assert (outcome.ok, outcome.code) == (False, "no_slot")
        assert "名额已用尽" in outcome.reply


class TestCreateAndJoin:
    async def test_create_reports_name_quota_and_broadcast(self, ctx):
        _, svc, _, _ = ctx
        outcome = await svc.create(GROUP, _pid("甲"), "甲", capacity=3)

        assert (outcome.ok, outcome.code) == (True, "ok")
        assert TEAM_NAME in outcome.reply
        assert "名额：1 支，已用 0 支" in outcome.reply
        assert len(outcome.broadcasts) == 1
        assert "甲" in outcome.broadcasts[0] and TEAM_NAME in outcome.broadcasts[0]

    async def test_create_rejects_bad_capacity(self, ctx):
        _, svc, _, _ = ctx
        assert (await svc.create(GROUP, _pid("甲"), "甲", capacity=1)).code == "bad_capacity"
        too_big = await svc.create(GROUP, _pid("甲"), "甲", capacity=99)
        assert too_big.code == "bad_capacity" and "最多" in too_big.reply

    async def test_second_team_same_day_gets_suffix(self, ctx):
        _, svc, _, _ = ctx
        await svc.create(GROUP, _pid("甲"), "甲", capacity=3)
        second = await svc.create(GROUP, _pid("乙"), "乙", capacity=3)

        assert second.ok
        assert TEAM_NAME + "2" in second.reply

    async def test_join_departs_and_grants_status(self, ctx):
        db, svc, _, _ = ctx
        await svc.create(GROUP, _pid("甲"), "甲", capacity=2)
        outcome = await svc.join(GROUP, _pid("乙"), "乙")

        assert (outcome.ok, outcome.code) == (True, "departed")
        assert len(outcome.broadcasts) == 1
        for who in ["甲", "乙"]:
            assert [s["status_name"] for s in await _statuses(db, who)] == [TEAM_NAME]

    async def test_join_without_name_prefers_recruiting(self, ctx):
        """不带队名时先帮还在招募的队伍凑满，而不是去补已发车的空位。"""
        db, svc, _, clock = ctx
        await svc.create(GROUP, _pid("甲"), "甲", capacity=2)
        await svc.join(GROUP, _pid("乙"), "乙")  # 发车
        await svc.admin_remove_member(GROUP, TEAM_NAME, "乙")  # 已发车队伍空出一个位置

        # 同一名额日期的额度已被用掉，所以换一天再开一支在招募的
        clock.value += timedelta(days=1)
        recruiting = await svc.create(GROUP, _pid("丙"), "丙", capacity=3)
        assert recruiting.ok, recruiting.reply

        outcome = await svc.join(GROUP, _pid("丁"), "丁")
        assert outcome.code == "joined"
        assert "丙" in outcome.reply, "没挑还在招募的那支"
        assert "补位" not in outcome.reply

    async def test_join_by_name_and_unknown(self, ctx):
        _, svc, _, _ = ctx
        await svc.create(GROUP, _pid("甲"), "甲", capacity=3)

        hit = await svc.join(GROUP, _pid("乙"), "乙", team_name=TEAM_NAME)
        assert hit.code == "joined"
        miss = await svc.join(GROUP, _pid("丙"), "丙", team_name="不存在")
        assert (miss.ok, miss.code) == (False, "not_found")

    async def test_join_without_candidates(self, ctx):
        _, svc, _, _ = ctx
        outcome = await svc.join(GROUP, _pid("甲"), "甲")
        assert outcome.code == "no_candidates"
        assert "祈愿组队" in outcome.reply

    async def test_name_conflict_message_has_way_out(self, ctx):
        db, svc, _, _ = ctx
        await svc.create(GROUP, _pid("甲"), "甲", capacity=3)
        await db.add_status(GROUP, _pid("乙"), TEAM_NAME, 5)
        await db.commit()

        outcome = await svc.join(GROUP, _pid("乙"), "乙")
        assert outcome.code == "name_conflict"
        assert "重命名状态" in outcome.reply

    async def test_sibling_voided_on_departure(self, ctx):
        """名额被抢走后，同日的另一支队立刻被判「未能成行」并播报。"""
        db, svc, _, _ = ctx
        await svc.create(GROUP, _pid("甲"), "甲", capacity=2)
        await svc.create(GROUP, _pid("丙"), "丙", capacity=3)

        outcome = await svc.join(GROUP, _pid("乙"), "乙")  # 第一支发车
        assert outcome.code == "departed"
        assert len(outcome.broadcasts) == 2, "只播报了发车，没有宣判另一支队"

        sibling = await _team(db, TEAM_NAME + "2")
        assert sibling["status"] == WISH_VOIDED
        assert [s["status_name"] for s in await _statuses(db, "丙")] == []

        refused = await svc.join(GROUP, _pid("丁"), "丁", team_name=TEAM_NAME + "2")
        assert refused.code == "voided"


class TestMineAndRandom:
    async def test_mine_reports_role_and_window(self, ctx):
        _, svc, _, _ = ctx
        await svc.create(GROUP, _pid("甲"), "甲", capacity=2)
        await svc.join(GROUP, _pid("乙"), "乙")

        leader = await svc.mine(GROUP, _pid("甲"))
        assert "队长" in leader.reply and "到期" in leader.reply
        assert "队员" in (await svc.mine(GROUP, _pid("乙"))).reply

    async def test_mine_without_team(self, ctx):
        _, svc, _, _ = ctx
        outcome = await svc.mine(GROUP, _pid("甲"))
        assert (outcome.ok, outcome.code) == (False, "not_found")
        assert "没有参加" in outcome.reply

    async def test_random_self_blocked_in_real_team(self, ctx):
        _, svc, _, _ = ctx
        await svc.create(GROUP, _pid("甲"), "甲", capacity=3)
        await svc.join(GROUP, _pid("乙"), "乙")

        outcome = await svc.random_self(GROUP, _pid("乙"), "乙")
        assert outcome.code == "in_team"
        assert "只有你一人" in outcome.reply

    async def test_random_self_joins_most_needy(self, ctx):
        _, svc, _, _ = ctx
        await svc.create(GROUP, _pid("甲"), "甲", capacity=4)
        await svc.join(GROUP, _pid("乙"), "乙")

        outcome = await svc.random_self(GROUP, _pid("丙"), "丙")
        assert outcome.code == "joined"
        assert "3/4" in outcome.reply


class TestRoster:
    async def test_plain_roster_without_scores(self, ctx):
        _, svc, _, _ = ctx
        await svc.create(GROUP, _pid("甲"), "甲", capacity=2)
        await svc.join(GROUP, _pid("乙"), "乙")

        outcome = await svc.admin_roster(GROUP, TEAM_NAME)
        assert outcome.code == "plain"
        assert "甲" in outcome.reply and "乙" in outcome.reply
        assert "批量录入" in outcome.reply

    async def test_batch_roster_parses(self, ctx):
        db, svc, _, _ = ctx
        await svc.create(GROUP, _pid("甲"), "甲", capacity=2)
        await svc.join(GROUP, _pid("乙"), "乙")

        outcome = await svc.admin_roster(GROUP, TEAM_NAME, ladder_delta=16, pilgrimage_delta=2)
        assert outcome.code == "batch"

        # 关键：产出的文本要真能被「批量录入」解出来（裸名单会被整块丢弃）
        blocks = outcome.reply.split("：\n", 1)[1]
        parsed, error = LadderService(db).parse_batch_scores(blocks)
        assert error is None
        assert [item["name"] for item in parsed] == ["甲", "乙"]
        assert {item["ladder_delta"] for item in parsed} == {16}
        assert {item["pilgrimage_delta"] for item in parsed} == {2}

    async def test_roster_unknown_team(self, ctx):
        _, svc, _, _ = ctx
        assert (await svc.admin_roster(GROUP, "没有这队")).code == "not_found"


class TestAdminOps:
    async def test_list_excludes_disbanded(self, ctx):
        _, svc, _, _ = ctx
        await svc.create(GROUP, _pid("甲"), "甲", capacity=2)
        await svc.admin_rename(GROUP, TEAM_NAME, "深渊队")
        await svc.admin_disband(GROUP, "深渊队")
        await svc.create(GROUP, _pid("乙"), "乙", capacity=2)  # 拿到默认名

        listed = await svc.admin_list(GROUP)
        assert TEAM_NAME in listed.reply
        assert "深渊队" not in listed.reply

        gone = await svc.admin_list_disbanded(GROUP)
        assert "深渊队" in gone.reply
        assert TEAM_NAME not in gone.reply

    async def test_disband_revokes_statuses(self, ctx):
        db, svc, _, _ = ctx
        await svc.create(GROUP, _pid("甲"), "甲", capacity=2)
        await svc.join(GROUP, _pid("乙"), "乙")
        assert await _statuses(db, "甲")

        outcome = await svc.admin_disband(GROUP, TEAM_NAME)
        assert outcome.ok and "撤销" in outcome.reply
        assert await _statuses(db, "甲") == []

    async def test_rename_syncs_status_name(self, ctx):
        db, svc, _, _ = ctx
        await svc.create(GROUP, _pid("甲"), "甲", capacity=2)
        await svc.join(GROUP, _pid("乙"), "乙")

        outcome = await svc.admin_rename(GROUP, TEAM_NAME, "深渊队")
        assert outcome.ok
        assert "改名 2 人" in outcome.reply
        assert [s["status_name"] for s in await _statuses(db, "甲")] == ["深渊队"]

    async def test_rename_rejections(self, ctx):
        _, svc, _, _ = ctx
        await svc.create(GROUP, _pid("甲"), "甲", capacity=2)
        await svc.create(GROUP, _pid("乙"), "乙", capacity=2)

        assert (await svc.admin_rename(GROUP, TEAM_NAME, TEAM_NAME + "2")).code == "name_taken"
        assert (await svc.admin_rename(GROUP, TEAM_NAME, "特别长的队名" * 4)).code == "too_long"
        assert (await svc.admin_rename(GROUP, TEAM_NAME, " ")).code == "empty_name"
        assert (await svc.admin_rename(GROUP, "没有这队", "新名")).code == "not_found"

    async def test_remove_member_promotes(self, ctx):
        db, svc, _, _ = ctx
        await svc.create(GROUP, _pid("甲"), "甲", capacity=4)
        await svc.join(GROUP, _pid("乙"), "乙")

        outcome = await svc.admin_remove_member(GROUP, TEAM_NAME, "甲")
        assert outcome.ok
        assert "队长已交给 乙" in outcome.reply

        not_in = await svc.admin_remove_member(GROUP, TEAM_NAME, "丙")
        assert not_in.code == "not_member"

    async def test_swap_transfers_and_broadcasts(self, ctx):
        db, svc, _, _ = ctx
        await svc.create(GROUP, _pid("甲"), "甲", capacity=2)
        await svc.join(GROUP, _pid("乙"), "乙")
        expiry = (await _team(db))["expire_at"]

        outcome = await svc.admin_swap(GROUP, TEAM_NAME, "乙", "丙")
        assert outcome.ok
        assert await _statuses(db, "乙") == []
        assert [s["expire_at"] for s in await _statuses(db, "丙")] == [expiry]
        assert len(outcome.broadcasts) == 1 and "丙" in outcome.broadcasts[0]

    async def test_swap_reports_already_member(self, ctx):
        """接替者已经在队里 —— 要给出可读文案，而不是把结果码当回复甩出去。"""
        _, svc, _, _ = ctx
        await svc.create(GROUP, _pid("甲"), "甲", capacity=3)
        await svc.join(GROUP, _pid("乙"), "乙")

        outcome = await svc.admin_swap(GROUP, TEAM_NAME, "乙", "甲")
        assert outcome.code == "already_member"
        assert "已经在这支队里" in outcome.reply

    async def test_swap_reports_busy_replacement(self, ctx):
        _, svc, _, clock = ctx
        await svc.create(GROUP, _pid("甲"), "甲", capacity=2)
        await svc.join(GROUP, _pid("乙"), "乙")

        # 同一名额日期的额度已被用掉，换一天再开一支把丙放进去
        clock.value += timedelta(days=1)
        assert (await svc.create(GROUP, _pid("丙"), "丙", capacity=3)).ok

        outcome = await svc.admin_swap(GROUP, TEAM_NAME, "乙", "丙")
        assert outcome.code == "in_has_team"
        assert "别的队伍" in outcome.reply

    async def test_extend_only_extends(self, ctx):
        db, svc, _, _ = ctx
        await svc.create(GROUP, _pid("甲"), "甲", capacity=2)
        await svc.join(GROUP, _pid("乙"), "乙")
        before = (await _team(db))["expire_at"]

        assert (await svc.admin_extend(GROUP, TEAM_NAME, 0)).code == "invalid_days"
        assert (await svc.admin_extend(GROUP, TEAM_NAME, -1)).code == "invalid_days"

        outcome = await svc.admin_extend(GROUP, TEAM_NAME, 2)
        assert outcome.ok
        assert [s["expire_at"] for s in await _statuses(db, "甲")][0] > before

    async def test_extend_refuses_recruiting_team(self, ctx):
        _, svc, _, _ = ctx
        await svc.create(GROUP, _pid("甲"), "甲", capacity=3)
        assert (await svc.admin_extend(GROUP, TEAM_NAME, 1)).code == "not_departed"

    async def test_fill_member_can_depart(self, ctx):
        db, svc, _, _ = ctx
        await svc.create(GROUP, _pid("甲"), "甲", capacity=2)

        outcome = await svc.admin_fill_member(GROUP, TEAM_NAME, "乙")
        assert outcome.code == "departed"
        assert [s["status_name"] for s in await _statuses(db, "乙")] == [TEAM_NAME]

    async def test_clear_releases_quota(self, ctx):
        db, svc, _, _ = ctx
        await svc.create(GROUP, _pid("甲"), "甲", capacity=2)
        await svc.join(GROUP, _pid("乙"), "乙")

        outcome = await svc.admin_clear(GROUP)
        assert outcome.ok and "当天名额已释放" in outcome.reply
        assert await db.slot_usage(GROUP, svc.slot_date_of()) == 0

    async def test_unknown_player_message(self, ctx):
        _, svc, _, _ = ctx
        await svc.create(GROUP, _pid("甲"), "甲", capacity=2)
        outcome = await svc.admin_remove_member(GROUP, TEAM_NAME, "路人")
        assert outcome.code == "player_not_found"

    async def test_leave_leader_disbands(self, ctx):
        db, svc, _, _ = ctx
        await svc.create(GROUP, _pid("甲"), "甲", capacity=3)
        outcome = await svc.leave(GROUP, _pid("甲"))

        assert outcome.code == "disbanded" and "队长" in outcome.reply
        assert (await _team(db))["status"] == WISH_DISBANDED

    async def test_leave_without_team(self, ctx):
        _, svc, _, _ = ctx
        outcome = await svc.leave(GROUP, _pid("甲"))
        assert outcome.code == "not_found"
        assert "祈愿管理" in outcome.reply


class TestTick:
    async def test_no_reminder_right_after_create(self, ctx):
        _, svc, _, _ = ctx
        await svc.create(GROUP, _pid("甲"), "甲", capacity=3)
        assert await svc.tick() == [], "开团播报之后立刻又来一条提醒"

    async def test_reminder_fires_once_per_window(self, ctx):
        db, svc, _, _ = ctx
        await svc.create(GROUP, _pid("甲"), "甲", capacity=3)
        await _backdate(db, (await _team(db))["team_id"])

        first = await svc.tick()
        assert len(first) == 1
        assert first[0][0] == GROUP
        assert TEAM_NAME in first[0][1]
        assert "名额" in first[0][1]

        assert await svc.tick() == [], "同一个提醒窗口被提醒了两次"

    async def test_timeout_disbands_and_broadcasts(self, ctx):
        db, svc, _, _ = ctx
        await svc.create(GROUP, _pid("甲"), "甲", capacity=3)
        team = await _team(db)
        await _backdate(db, team["team_id"], "created_at")

        out = await svc.tick()
        assert len(out) == 1 and out[0][0] == GROUP
        assert (await db.get_wish_team(team["team_id"]))["status"] == WISH_DISBANDED

    async def test_timeout_stays_silent_for_empty_team(self, ctx):
        db, svc, _, _ = ctx
        await svc.create(GROUP, _pid("甲"), "甲", capacity=3)
        team = await _team(db)
        await db._db.execute(
            "DELETE FROM wish_team_members WHERE team_id = ?", (team["team_id"],)
        )
        await db._db.commit()
        await _backdate(db, team["team_id"], "created_at")

        assert await svc.tick() == []

    async def test_tick_ignores_unlisted_group(self, ctx):
        db, svc, _, _ = ctx
        await svc.create(OTHER_GROUP, _pid("甲"), "甲", capacity=3)
        await _backdate(db, (await _team(db, group=OTHER_GROUP))["team_id"])

        assert await svc.tick() == []

    async def test_tick_can_be_disabled(self, ctx):
        db, svc, config, _ = ctx
        await svc.create(GROUP, _pid("甲"), "甲", capacity=3)
        team = await _team(db)
        await _backdate(db, team["team_id"])
        await _backdate(db, team["team_id"], "created_at")

        config["wish_reminder_minutes"] = 0
        config["wish_disband_minutes"] = 0

        assert await svc.tick() == []
        assert (await db.get_wish_team(team["team_id"]))["status"] == WISH_RECRUITING


class TestMemberLeave:
    async def test_leave_removes_from_departed_team(self, ctx):
        db, svc, _, _ = ctx
        await svc.create(GROUP, _pid("甲"), "甲", capacity=2)
        await svc.join(GROUP, _pid("乙"), "乙")

        out = await svc.handle_member_leave(GROUP, _pid("乙"))
        assert len(out) == 1 and out[0][0] == GROUP
        assert await _statuses(db, "乙") == []
        assert [m["player_name"] for m in (await _team(db))["members"]] == ["甲"]

    async def test_leave_of_leader_disbands(self, ctx):
        db, svc, _, _ = ctx
        await svc.create(GROUP, _pid("甲"), "甲", capacity=3)

        out = await svc.handle_member_leave(GROUP, _pid("甲"))
        assert "解散" in out[0][1]
        assert (await _team(db))["status"] == WISH_DISBANDED

    async def test_leave_without_team_is_silent(self, ctx):
        _, svc, _, _ = ctx
        assert await svc.handle_member_leave(GROUP, _pid("甲")) == []
