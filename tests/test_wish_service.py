"""
祈愿试炼服务层测试。

除常规流程外，这里专门钉两条最容易「改着改着就没人发现」的东西：

1. **星期名额表是按队名日期（= 发起日 + 状态天数）反推的**。默认 `[1,1,0,1,1,0,2]`
   的实际效果是「周三与周日不能发起、周四 2 支、其余 1 支，每周最多 6 次发车」。
   谁要是把口径改成「按发起当天」，这条会立刻红。
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
# 夹具把时钟定在 2026-09-25（周五）：名额日期 = 发起日 + 3 天 = 09-28（周一），
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
        """反推「发起日是周几 → 能开几支」，把口径钉死。

        取连续 7 天即可覆盖全部星期几，不必先对齐到周一。
        """
        _, svc, _, clock = ctx
        by_weekday = {}
        for offset in range(7):
            day = clock.value + timedelta(days=offset)
            by_weekday[WEEKDAY_LABELS[day.weekday()]] = svc.weekly_slot_limit(svc.slot_date_of(day))

        assert by_weekday == {
            "周一": 1, "周二": 1, "周三": 0, "周四": 2, "周五": 1, "周六": 1, "周日": 0,
        }, "星期名额表的口径变了（默认按队名日期算，不是按发起当天）"
        assert sum(by_weekday.values()) == 6, "每周最多 6 次发车"

    def test_slot_date_carries_weekday_label(self, ctx):
        _, svc, _, _ = ctx
        assert svc.format_slot("2026-09-28") == "09月28日（周一）"

    async def test_closed_day_blocks_create(self, ctx):
        _, svc, _, clock = ctx
        clock.jump_to_weekday("周三")  # 周三发起 → 队名日期落在周六（关闭）

        outcome = await svc.create(GROUP, _pid("甲"), "甲")
        assert (outcome.ok, outcome.code) == (False, "closed_day")
        assert "队名日期会落在周六" in outcome.reply
        assert "不安排周三、周六" in outcome.reply
        assert "下一次可发起的日子" in outcome.reply

    async def test_closed_day_copy_follows_weekly_table(self, ctx):
        """关闭日的文案必须跟着名额表走：改了表而文案不变，玩家就会照错的规则理解。"""
        _, svc, config, clock = ctx
        config["wish_weekly_limits"] = [1, 1, 1, 1, 1, 0, 2]  # 只关周六

        assert svc.closed_weekdays_text() == "周六"
        clock.jump_to_weekday("周三")  # 队名日期落在周六
        outcome = await svc.create(GROUP, _pid("甲"), "甲")
        assert "不安排周六的祈愿试炼" in outcome.reply
        assert "周三" not in outcome.reply

    def test_next_open_day_skips_closed_slots(self, ctx):
        _, svc, _, clock = ctx
        clock.jump_to_weekday("周三")  # 今天关（队名日期周六）
        nxt = svc.next_open_day()

        assert nxt == (clock.value + timedelta(days=1)).strftime("%Y-%m-%d")
        assert svc.weekly_slot_limit(svc.slot_date_of(datetime.strptime(nxt, "%Y-%m-%d"))) > 0

    async def test_hall_says_closed_on_closed_day(self, ctx):
        _, svc, _, clock = ctx
        clock.jump_to_weekday("周三")

        outcome = await svc.list_open(GROUP)
        assert "今天无法发起祈愿" in outcome.reply
        assert "不安排周三、周六" in outcome.reply
        assert "下一次" in outcome.reply

    async def test_hall_says_open_today(self, ctx):
        _, svc, _, clock = ctx
        clock.jump_to_weekday("周一")  # 周一发起 → 队名日期周四，允许 1 支
        opened = await svc.list_open(GROUP)
        assert "今天可以发起祈愿" in opened.reply
        assert "名额：1 支，已用 0 支" in opened.reply

    async def test_hall_says_closed_when_quota_used_up(self, ctx):
        """名额用尽后大厅也要直说不能再开，而不是只报「已用 1 支」。"""
        _, svc, _, _ = ctx
        await svc.create(GROUP, _pid("甲"), "甲", capacity=2)
        await svc.join(GROUP, _pid("乙"), "乙")  # 发车，用掉唯一名额

        outcome = await svc.list_open(GROUP)
        assert "今天无法发起祈愿" in outcome.reply
        assert "名额已用尽" in outcome.reply
        assert "下一次" in outcome.reply

    async def test_hall_still_open_when_quota_partially_used(self, ctx):
        """名额还没用完（默认表里周四那支日期有 2 个名额）时仍要报「可以发起」。"""
        _, svc, _, clock = ctx
        clock.jump_to_weekday("周四")
        assert svc.weekly_slot_limit(svc.slot_date_of()) == 2

        await svc.create(GROUP, _pid("甲"), "甲", capacity=2)
        await svc.join(GROUP, _pid("乙"), "乙")

        outcome = await svc.list_open(GROUP)
        assert "今天可以发起祈愿" in outcome.reply
        assert "已用 1 支" in outcome.reply

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

    async def test_many_joins_complete_one_team_instead_of_splitting(self, ctx):
        """多人依次加入时要把一支凑满，而不是在两支之间摊平。

        这是「均衡」策略的致命处：按人最少挑会让人交替流动——两支各 1 人时来 6 个人，
        会补成 4/6 和 3/6，两支都发不了车、双双超时。按「差得最少」挑，同样这批人
        里前 5 个就能换来一场发车。
        """
        db, svc, _, _ = ctx
        first = (await svc.create(GROUP, _pid("甲"), "甲", capacity=6)).reply
        second = await svc.create(GROUP, _pid("乙"), "乙", capacity=6)
        assert "09月28日祈愿试炼" in first and "09月28日祈愿试炼2" in second.reply
        second_id = (await _team(db, "09月28日祈愿试炼2"))["team_id"]

        codes = []
        for who in ["丙", "丁", "戊", "己", "庚", "辛"]:
            codes.append((await svc.join(GROUP, _pid(who), who)).code)

        assert codes.count("departed") == 1, f"没有（或不止一支）凑满发车：{codes}"
        assert await db.slot_usage(GROUP, svc.slot_date_of()) == 1

        departed = await db.list_wish_teams(GROUP, (WISH_DEPARTED,))
        assert len(departed) == 1
        assert len(departed[0]["members"]) == 6, "发车那支应当正好满员"
        # 名额被抢走后，同日另一支立刻被判「未能成行」
        voided = await db.list_wish_teams(GROUP, (WISH_VOIDED,))
        assert [t["team_id"] for t in voided] == [second_id]

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

    async def test_stats_summarises_window(self, ctx):
        """统计要能回答「发起多不多、发车成不成、卡在哪一步」。"""
        _, svc, _, clock = ctx
        # 第一支：发车；第二支：名额被抢 → 未能成行；第三支（换一天）：诸神解散 → 没凑够
        await svc.create(GROUP, _pid("甲"), "甲", capacity=2)
        await svc.create(GROUP, _pid("丙"), "丙", capacity=3)
        await svc.join(GROUP, _pid("乙"), "乙")
        clock.value += timedelta(days=1)
        day2_name = svc.default_team_name(svc.slot_date_of())  # 换天后的队名带的是新日期
        await svc.create(GROUP, _pid("丁"), "丁", capacity=3)
        await svc.admin_disband(GROUP, day2_name)

        outcome = await svc.admin_stats(GROUP, 7)
        assert outcome.ok
        assert "发起 3 次" in outcome.reply
        assert "发车 1" in outcome.reply
        assert "未能成行 1" in outcome.reply
        assert "没凑够 1" in outcome.reply
        assert "参与 4 人次，去重 4 人" in outcome.reply
        assert "发车率 33%" in outcome.reply
        assert "满员率 100%" in outcome.reply

    async def test_stats_empty_window(self, ctx):
        _, svc, _, _ = ctx
        outcome = await svc.admin_stats(GROUP, 7)
        assert "没有开过团" in outcome.reply

    async def test_stats_respects_window(self, ctx):
        """只统计窗口内发起的队伍。"""
        _, svc, _, clock = ctx
        await svc.create(GROUP, _pid("甲"), "甲", capacity=3)
        clock.value += timedelta(days=10)

        assert "没有开过团" in (await svc.admin_stats(GROUP, 7)).reply
        assert "发起 1 次" in (await svc.admin_stats(GROUP, 30)).reply

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
        assert await svc.tick() == [], "发起播报之后立刻又来一条提醒"

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


class TestQuotaSemantics:
    """上限只由成功发车产生（v3.9.2）：发起不限次、失败不留痕、解散即释放。"""

    async def test_only_departure_consumes_quota(self, ctx):
        """发起、主动解散、超时解散都不占名额——只有发车才占。"""
        db, svc, _, _ = ctx
        await svc.create(GROUP, _pid("甲"), "甲", capacity=3)
        assert await db.slot_usage(GROUP, svc.slot_date_of()) == 0, "发起就占了名额"

        await svc.leave(GROUP, _pid("甲"))
        assert await db.slot_usage(GROUP, svc.slot_date_of()) == 0, "解散没有释放名额"

        await svc.create(GROUP, _pid("乙"), "乙", capacity=3)
        team = (await db.get_open_wish_teams(GROUP))[0]
        await _backdate(db, team["team_id"], "created_at")
        await svc.tick()
        assert (await db.get_wish_team(team["team_id"]))["status"] == WISH_DISBANDED
        assert await db.slot_usage(GROUP, svc.slot_date_of()) == 0, "超时解散没有释放名额"

    async def test_tick_voids_team_when_slot_quota_closes(self, ctx):
        """诸神中途把某个日期的名额改成 0 之后，已存在的招募队伍由 tick 宣判，不等超时。"""
        db, svc, config, _ = ctx
        await svc.create(GROUP, _pid("甲"), "甲", capacity=3)
        # 夹具时钟是周五 → 今天对应的队名日期是周一 → 把周一改成 0
        config["wish_weekly_limits"] = [0, 1, 0, 1, 1, 0, 2]

        out = await svc.tick()
        assert out, "宣判了却没有播报"
        assert await db.get_open_wish_teams(GROUP) == []
        assert await db.list_wish_teams(GROUP, (WISH_VOIDED,)), "队伍没有被判为未能成行"

    async def test_void_sweep_ignores_reminder_switch(self, ctx):
        """兜底不受提醒开关影响。

        若把兜底写进 `if remind > 0` 分支，诸神把 `wish_reminder_minutes` 设成 0 就
        会连带关掉它，而且不报错——这条专门钉住那个写法。
        """
        db, svc, config, _ = ctx
        config["wish_reminder_minutes"] = 0
        await svc.create(GROUP, _pid("甲"), "甲", capacity=3)
        config["wish_weekly_limits"] = [0, 1, 0, 1, 1, 0, 2]

        out = await svc.tick()
        assert out, "关掉提醒之后名额兜底也失效了"
        assert await db.list_wish_teams(GROUP, (WISH_VOIDED,))

    async def test_join_refused_when_slot_closed(self, ctx):
        """名额为 0 的日期连加入都拒（否则队伍会照样加满并发车）。"""
        _, svc, config, _ = ctx
        await svc.create(GROUP, _pid("甲"), "甲", capacity=3)
        config["wish_weekly_limits"] = [0, 1, 0, 1, 1, 0, 2]

        outcome = await svc.join(GROUP, _pid("乙"), "乙")
        assert outcome.code == "closed_day"
        assert "不安排" in outcome.reply


class TestSlotBonus:
    """「仅今天加开」：只影响今天对应的那个队名日期，次日自动失效。"""

    async def test_bonus_raises_quota_and_join_honours_it(self, ctx):
        """加开必须同时被 create 与 join 认到。

        只改 create 那一路会造出「发起放行、加入被拦、还不被 tick 宣判」的卡死队伍
        （兜底走 slot_quota，会认为它未超限）。
        """
        _, svc, _, _ = ctx
        await svc.create(GROUP, _pid("甲"), "甲", capacity=2)
        await svc.join(GROUP, _pid("乙"), "乙")  # 发车，唯一名额用掉
        assert (await svc.slot_quota(GROUP, svc.slot_date_of()))[0] == 1
        assert (await svc.create(GROUP, _pid("丙"), "丙", capacity=2)).code == "no_slot"

        outcome = await svc.admin_bonus(GROUP, 1)
        assert outcome.ok and "加开 1 场" in outcome.reply
        assert await svc.slot_quota(GROUP, svc.slot_date_of()) == (2, 1)

        assert (await svc.create(GROUP, _pid("丙"), "丙", capacity=2)).ok
        filled = await svc.join(GROUP, _pid("丁"), "丁")
        assert filled.code == "departed", "加开之后 join 仍按旧上限拦人"
        assert await svc.slot_quota(GROUP, svc.slot_date_of()) == (2, 2)

    async def test_bonus_only_applies_to_today(self, ctx):
        """只写今天那个日期，所以次日自动失效（不需要过期逻辑）。"""
        _, svc, _, clock = ctx
        await svc.admin_bonus(GROUP, 2)
        today_slot = svc.slot_date_of()
        assert (await svc.slot_quota(GROUP, today_slot))[0] == (
            svc.weekly_slot_limit(today_slot) + 2
        )

        clock.value += timedelta(days=1)
        tomorrow_slot = svc.slot_date_of()
        assert tomorrow_slot != today_slot
        assert (await svc.slot_quota(GROUP, tomorrow_slot))[0] == svc.weekly_slot_limit(
            tomorrow_slot
        ), "昨天的加开泄漏到了今天"

    async def test_bonus_clear_and_cap(self, ctx):
        _, svc, _, _ = ctx
        await svc.admin_bonus(GROUP, 3)
        cleared = await svc.admin_bonus(GROUP, 0)
        assert cleared.ok and "已清除" in cleared.reply
        assert await svc.slot_bonus(GROUP, svc.slot_date_of()) == 0

        capped = await svc.admin_bonus(GROUP, 99)
        assert capped.code == "capped" and "最多" in capped.reply
        assert await svc.slot_bonus(GROUP, svc.slot_date_of()) == 0, "被拒的加开改动了已有值"

    async def test_hall_shows_bonus(self, ctx):
        """名额从此有两个来源，大厅必须显示出加开那一份。"""
        _, svc, _, _ = ctx
        await svc.admin_bonus(GROUP, 1)

        outcome = await svc.list_open(GROUP)
        assert "含今日加开 1" in outcome.reply


class TestBroadcastThrottle:
    async def test_same_person_repeat_is_not_announced(self, ctx):
        """同一人在窗口内重复发起：发起照常成功，只是不再播报。"""
        _, svc, config, clock = ctx
        first = await svc.create(GROUP, _pid("甲"), "甲", capacity=3)
        assert len(first.broadcasts) == 1

        await svc.leave(GROUP, _pid("甲"))
        second = await svc.create(GROUP, _pid("甲"), "甲", capacity=3)
        assert second.ok, "节流不该影响发起本身"
        assert second.broadcasts == [] and "不再播报" in second.reply

        # 换个人不受影响
        third = await svc.create(GROUP, _pid("乙"), "乙", capacity=3)
        assert len(third.broadcasts) == 1

        # 过了窗口就恢复
        clock.value += timedelta(seconds=svc.broadcast_throttle_seconds() + 1)
        await svc.leave(GROUP, _pid("甲"))
        fourth = await svc.create(GROUP, _pid("甲"), "甲", capacity=3)
        assert len(fourth.broadcasts) == 1, "过了窗口仍不播报"

    async def test_throttle_can_be_disabled(self, ctx):
        _, svc, config, _ = ctx
        config["wish_broadcast_throttle_seconds"] = 0

        await svc.create(GROUP, _pid("甲"), "甲", capacity=3)
        await svc.leave(GROUP, _pid("甲"))
        again = await svc.create(GROUP, _pid("甲"), "甲", capacity=3)
        assert len(again.broadcasts) == 1
