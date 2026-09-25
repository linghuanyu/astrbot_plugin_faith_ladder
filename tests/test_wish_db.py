"""
祈愿试炼数据层（`db_manager` 的祈愿试炼一节）测试。

最容易写坏的几处不变式，这里逐个钉死：

- 队伍 `expire_at` 是**全队状态的唯一事实来源**：发车、补位、换人三条授予路径
  都必须写出同一个到期时间，否则队友之间会漂开（补位进来的人比队友晚一天）。
- 「名额只认领一次」：条件更新 + rowcount，并发下只有一方能发车。
- 名单驱动：移出 / 解散要撤销状态，补位 / 换人要按队伍到期时间挂上。
- 前置检查失败**不消耗当天名额**（占名额是最后一步）。
"""

import pytest
import pytest_asyncio

from astrbot_plugin_faith_ladder.db_manager import (
    STATUS_SOURCE_WISH,
    WISH_DEPARTED,
    WISH_DISBANDED,
    WISH_RECRUITING,
    WISH_VOIDED,
    DatabaseManager,
)

GROUP = "g1"
SLOT = "2026-09-28"
CREATE_DAY = "2026-09-25"
TEAM_NAME = "09月28日祈愿试炼"

# 默认限额：群内同时 2 支招募中、每人每天 1 次、本群每天 4 次
LIMITS = dict(recruiting_limit=2)


@pytest_asyncio.fixture
async def db(temp_data_dir):
    dbm = DatabaseManager(temp_data_dir)
    await dbm.initialize()
    yield dbm
    await dbm.close()


async def _create(db, leader="甲", capacity=3, name=TEAM_NAME, recruiting_limit=2):
    return await db.create_wish_team(
        GROUP, name, capacity, f"name:{leader}", leader, SLOT, CREATE_DAY,
        recruiting_limit,
    )


async def _join(db, team_id, who, capacity=3, slot_limit=1):
    return await db.join_wish_team(
        team_id, GROUP, f"name:{who}", who, slot_limit=slot_limit,
        status_days=3, keep_statuses=3,
    )


async def _statuses(db, who):
    return await db.get_player_statuses(GROUP, f"name:{who}")


async def _member_names(db, team_id):
    team = await db.get_wish_team(team_id)
    return [m["player_name"] for m in team["members"]]


async def _fill(db, team_id, members, capacity=3, slot_limit=1):
    """依次加入，返回最后一次的 (code, team)。"""
    result = None
    for who in members:
        result = await _join(db, team_id, who, capacity=capacity, slot_limit=slot_limit)
    return result


class TestCreate:
    async def test_create_seats_leader(self, db):
        team_id, code = await _create(db)
        assert (team_id, code) == (1, "ok")

        team = await db.get_wish_team(team_id)
        assert team["status"] == WISH_RECRUITING
        assert team["expire_at"] is None, "招募中还没有到期时间"
        assert [m["player_name"] for m in team["members"]] == ["甲"]
        assert await db.get_open_wish_teams(GROUP) == [team]

    async def test_second_team_same_day_gets_suffix(self, db):
        await _create(db, leader="甲")
        team_id, code = await _create(db, leader="乙")

        assert code == "ok"
        assert (await db.get_wish_team(team_id))["name"] == TEAM_NAME + "2"

    async def test_leader_cannot_open_twice(self, db):
        await _create(db, leader="甲")
        _, code = await _create(db, leader="甲", name="第二支")
        assert code == "already_in_team"

    async def test_recruiting_limit_blocks_new_team(self, db):
        await _create(db, leader="甲")
        await _create(db, leader="乙")

        _, code = await _create(db, leader="丙", name="第三支")
        assert code == "too_many_teams"

    async def test_create_is_not_rationed_by_count(self, db):
        """v3.9.2 起「上限」只由成功发车产生：同一个人可以反复发起。

        这正是旧版会失败的用例——原来「每人每天 1 次」，解散后再发起会被 `daily_limit`
        挡住（而且被本群上限拒绝时还会白白扣掉那次机会）。现在发起不限次。
        """
        first, code = await _create(db, leader="甲")
        assert code == "ok"
        leave_code, _team = await db.leave_wish_team(GROUP, "name:甲")
        assert leave_code == "disbanded"

        second, code = await _create(db, leader="甲", name="再发起一次")
        assert code == "ok", "解散后再次发起被拒了——「按次数」的限制没有真的删掉"
        assert second != first

    async def test_rejected_create_leaves_no_trace(self, db):
        """被前置检查拒绝的发起什么都不该留下（名额、队伍、成员都没有）。"""
        await _create(db, leader="甲")
        await _create(db, leader="乙")  # 群内招募中已达上限 2

        _, code = await _create(db, leader="丙", name="第三支")
        assert code == "too_many_teams"
        # 被拒后解散一支，丙立刻就能发起（没有任何次数被记下）
        await db.leave_wish_team(GROUP, "name:甲")
        team_id, code = await _create(db, leader="丙", name="第三支")
        assert code == "ok" and team_id is not None


class TestJoinAndDepart:
    async def test_join_until_depart_grants_same_expiry(self, db):
        team_id, _ = await _create(db)

        code, team = await _join(db, team_id, "乙")
        assert code == "joined"
        assert await _statuses(db, "乙") == [], "还没发车不该有状态"

        code, team = await _fill(db, team_id, ["丙"], capacity=3)
        assert code == "departed"
        assert team["status"] == WISH_DEPARTED

        expiry = team["expire_at"]
        assert expiry is not None
        for who in ["甲", "乙", "丙"]:
            statuses = await _statuses(db, who)
            assert [s["status_name"] for s in statuses] == [TEAM_NAME]
            assert statuses[0]["expire_at"] == expiry, f"{who} 的到期时间与队伍不一致"
            assert statuses[0]["source"] == STATUS_SOURCE_WISH
            assert statuses[0]["remaining_days"] == 3

    async def test_depart_consumes_slot_once(self, db):
        team_id, _ = await _create(db)
        await _fill(db, team_id, ["乙", "丙"])

        assert await db.slot_usage(GROUP, SLOT) == 1

        # 满员后再加入会被 capacity 拦住，名额不变
        code, _ = await _join(db, team_id, "丁")
        assert code == "full"
        assert await db.slot_usage(GROUP, SLOT) == 1

    async def test_already_member_is_ok(self, db):
        team_id, _ = await _create(db)
        code, _ = await _join(db, team_id, "甲")
        assert code == "already_member"

    async def test_name_conflict_blocks_join(self, db):
        """身上已有同名状态 → 加入被拒（规则：同名状态下不能加入）。"""
        team_id, _ = await _create(db)
        await db.add_status(GROUP, "name:乙", TEAM_NAME, 5, source=STATUS_SOURCE_WISH)
        await db.commit()

        code, _ = await _join(db, team_id, "乙")
        assert code == "name_conflict"

    async def test_same_slot_only_once(self, db):
        """同一名额日期只能参与一次（周四那支日期有 2 个名额，靠这条拦住同一个人）。"""
        first_id, _ = await _create(db, leader="甲")
        await _fill(db, first_id, ["乙", "丙"])

        second_id, _ = await _create(db, leader="丁", name="第二支")
        code, _ = await _join(db, second_id, "甲", slot_limit=2)
        assert code == "already_in_slot"

    async def test_one_team_at_a_time(self, db):
        first_id, _ = await _create(db, leader="甲")
        second_id, _ = await _create(db, leader="乙", name="第二支")

        code, _ = await _join(db, second_id, "甲")
        assert code == "already_in_other_team"

    async def test_no_slot_refuses_join(self, db):
        """名额已用尽时提前劝退，不让人白等 45 分钟。"""
        first_id, _ = await _create(db, leader="甲")
        await _fill(db, first_id, ["乙", "丙"])

        second_id, _ = await _create(db, leader="丁", name="第二支")
        code, _ = await _join(db, second_id, "戊", slot_limit=1)
        assert code == "no_slot"

    async def test_join_voided_or_disbanded_reports_status(self, db):
        team_id, _ = await _create(db)
        await db.disband_wish_team(team_id, GROUP)

        code, _ = await _join(db, team_id, "乙")
        assert code == WISH_DISBANDED


class TestWindowAndReplenish:
    async def test_replenish_matches_team_expiry(self, db):
        """补位进来的人必须与队友同一天到期（按队伍 expire_at 挂，不是 now+3）。"""
        team_id, _ = await _create(db)
        await _fill(db, team_id, ["乙", "丙"])
        expiry = (await db.get_wish_team(team_id))["expire_at"]

        # 诸神移出丙 → 空出一个位置
        await db.remove_wish_team_member(team_id, GROUP, "name:丙")
        assert await _statuses(db, "丙") == [], "移出应当撤销状态"

        code, team = await _join(db, team_id, "丁")
        assert code == "replenished"
        statuses = await _statuses(db, "丁")
        assert statuses[0]["expire_at"] == expiry, "补位者的到期时间与队友不一致"
        assert [m["player_name"] for m in team["members"]] == ["甲", "乙", "丁"]

    async def test_closed_window_refuses_join(self, db):
        team_id, _ = await _create(db)
        await _fill(db, team_id, ["乙", "丙"])
        await db.remove_wish_team_member(team_id, GROUP, "name:丙")

        # 把窗口推到过去
        await db._db.execute(
            "UPDATE wish_teams SET expire_at = '2000-01-01 00:00:00' WHERE id = ?", (team_id,)
        )
        await db._db.commit()

        code, _ = await _join(db, team_id, "丁")
        assert code == "window_closed"


class TestRosterChanges:
    async def test_swap_transfers_status(self, db):
        team_id, _ = await _create(db)
        await _fill(db, team_id, ["乙", "丙"])
        expiry = (await db.get_wish_team(team_id))["expire_at"]

        code, team = await db.swap_wish_team_member(
            team_id, GROUP, "name:乙", "name:丁", "丁", keep_statuses=3
        )
        assert code == "ok"
        assert await _statuses(db, "乙") == [], "被换下的人状态该撤销"
        assert [s["expire_at"] for s in await _statuses(db, "丁")] == [expiry], "状态没转移过去"
        assert [s["status_name"] for s in await _statuses(db, "丁")] == [TEAM_NAME]
        assert [m["player_name"] for m in team["members"]] == ["甲", "丙", "丁"]

    async def test_swap_leaves_roster_intact_when_replacement_is_busy(self, db):
        team_id, _ = await _create(db)
        await _fill(db, team_id, ["乙", "丙"])
        other_id, _ = await _create(db, leader="戊", name="第二支")
        # 名额已被第一支队用掉，所以这里的上限放开，只为把己放进另一支队
        await _join(db, other_id, "己", slot_limit=2)

        code, _ = await db.swap_wish_team_member(
            team_id, GROUP, "name:乙", "name:己", "己", keep_statuses=3
        )
        assert code == "in_has_team"
        assert await _member_names(db, team_id) == ["甲", "乙", "丙"], "失败的换人改了名单"

    async def test_swap_promotes_leader(self, db):
        team_id, _ = await _create(db)

        _, team = await db.swap_wish_team_member(
            team_id, GROUP, "name:甲", "name:乙", "乙", keep_statuses=3
        )
        assert (team["leader_id"], team["leader_name"]) == ("name:乙", "乙")

    async def test_remove_member_promotes_earliest(self, db):
        team_id, _ = await _create(db)
        await _join(db, team_id, "乙")
        await _join(db, team_id, "丙")

        _, team = await db.remove_wish_team_member(team_id, GROUP, "name:甲")
        assert (team["leader_id"], team["leader_name"]) == ("name:乙", "乙")

    async def test_remove_last_member_disbands(self, db):
        team_id, _ = await _create(db)
        _, team = await db.remove_wish_team_member(team_id, GROUP, "name:甲")
        assert team["status"] == WISH_DISBANDED

    async def test_disband_revokes_everyone(self, db):
        team_id, _ = await _create(db)
        await _fill(db, team_id, ["乙", "丙"])

        code, _ = await db.disband_wish_team(team_id, GROUP)
        assert code == "ok"
        for who in ["甲", "乙", "丙"]:
            assert await _statuses(db, who) == [], f"{who} 的状态没随解散撤销"

    async def test_leave_disbands_when_leader_leaves(self, db):
        team_id, _ = await _create(db)
        code, team = await db.leave_wish_team(GROUP, "name:甲")
        assert code == "disbanded"
        assert team["status"] == WISH_DISBANDED
        assert await db.get_open_wish_teams(GROUP) == []

    async def test_leave_ignores_departed_team(self, db):
        team_id, _ = await _create(db)
        await _fill(db, team_id, ["乙", "丙"])

        code, _ = await db.leave_wish_team(GROUP, "name:乙")
        assert code == "not_found", "已发车队伍不接受成员自行退出"
        assert await _member_names(db, team_id) == ["甲", "乙", "丙"]

class TestExtendAndRename:
    async def test_extend_moves_team_and_all_statuses(self, db):
        team_id, _ = await _create(db)
        await _fill(db, team_id, ["乙", "丙"])
        before = (await db.get_wish_team(team_id))["expire_at"]

        code, team = await db.extend_wish_team_expiry(team_id, GROUP, 2, keep_statuses=3)
        assert code == "ok"
        assert team["expire_at"] > before
        for who in ["甲", "乙", "丙"]:
            assert [s["expire_at"] for s in await _statuses(db, who)] == [team["expire_at"]]

    async def test_extend_rejects_non_positive(self, db):
        team_id, _ = await _create(db)
        assert (await db.extend_wish_team_expiry(team_id, GROUP, 0, 3))[0] == "invalid_days"
        assert (await db.extend_wish_team_expiry(team_id, GROUP, -1, 3))[0] == "invalid_days"

    async def test_extend_refuses_recruiting_team(self, db):
        team_id, _ = await _create(db)
        assert (await db.extend_wish_team_expiry(team_id, GROUP, 1, 3))[0] == "not_departed"

    async def test_rename_syncs_member_status_names(self, db):
        team_id, _ = await _create(db)
        await _fill(db, team_id, ["乙", "丙"])
        expiry = (await db.get_wish_team(team_id))["expire_at"]

        code, detail = await db.rename_wish_team(team_id, GROUP, "深渊队")
        assert (code, detail) == ("ok", {"renamed": 3, "merged": 0, "skipped": 0})

        for who in ["甲", "乙", "丙"]:
            statuses = await _statuses(db, who)
            assert [s["status_name"] for s in statuses] == ["深渊队"]
            assert statuses[0]["expire_at"] == expiry, "改名不该动到期时间"
        assert (await db.get_wish_team(team_id))["name"] == "深渊队"

    async def test_rename_rejects_taken_name(self, db):
        first, _ = await _create(db, leader="甲")
        second, _ = await _create(db, leader="乙")

        code, _ = await db.rename_wish_team(second, GROUP, TEAM_NAME)
        assert code == "name_taken"
        assert (await db.get_wish_team(first))["name"] == TEAM_NAME

    async def test_rename_rejects_empty(self, db):
        team_id, _ = await _create(db)
        assert (await db.rename_wish_team(team_id, GROUP, "   "))[0] == "empty_name"

    async def test_rename_does_not_touch_other_source_status(self, db):
        """诸神手写的同名状态不归队伍管，队伍改名不该把它搬走。"""
        team_id, _ = await _create(db)
        await _fill(db, team_id, ["乙", "丙"])
        await db.add_status(GROUP, "name:乙", "别的状态", 5)
        await db.commit()

        await db.rename_wish_team(team_id, GROUP, "深渊队")
        names = [s["status_name"] for s in await _statuses(db, "乙")]
        assert names == ["深渊队", "别的状态"]


class TestSlotAndCleanup:
    async def test_void_siblings_after_slot_runs_out(self, db):
        first, _ = await _create(db, leader="甲")
        second, _ = await _create(db, leader="丁", name="第二支")
        await _fill(db, first, ["乙", "丙"])  # 用掉唯一名额

        affected = await db.void_sibling_teams(GROUP, SLOT)
        assert [t["team_id"] for t in affected] == [second]
        assert (await db.get_wish_team(second))["status"] == WISH_VOIDED

    async def test_void_siblings_keeps_other_slot_dates(self, db):
        first, _ = await _create(db, leader="甲")
        other, _ = await db.create_wish_team(
            GROUP, "10月01日祈愿试炼", 3, "name:丁", "丁", "2026-10-01", CREATE_DAY, **LIMITS
        )
        await _fill(db, first, ["乙", "丙"])

        await db.void_sibling_teams(GROUP, SLOT)
        assert (await db.get_wish_team(other))["status"] == WISH_RECRUITING

    async def test_stale_disband_only_hits_old_recruiting(self, db):
        old, _ = await _create(db, leader="甲")
        fresh, _ = await _create(db, leader="乙", name="第二支")
        await db._db.execute(
            "UPDATE wish_teams SET created_at = '2000-01-01 00:00:00' WHERE id = ?", (old,)
        )
        await db._db.commit()

        affected = await db.disband_stale_wish_teams("2026-01-01 00:00:00")
        assert [t["team_id"] for t in affected] == [old]
        assert (await db.get_wish_team(old))["status"] == WISH_DISBANDED
        assert (await db.get_wish_team(fresh))["status"] == WISH_RECRUITING

    async def test_stale_disband_never_touches_departed(self, db):
        team_id, _ = await _create(db)
        await _fill(db, team_id, ["乙", "丙"])
        await db._db.execute(
            "UPDATE wish_teams SET created_at = '2000-01-01 00:00:00' WHERE id = ?", (team_id,)
        )
        await db._db.commit()

        assert await db.disband_stale_wish_teams("2026-01-01 00:00:00") == []
        assert (await db.get_wish_team(team_id))["status"] == WISH_DEPARTED
        assert await _statuses(db, "甲"), "超时清理不该碰已发车队伍的状态"

    async def test_fresh_team_is_not_claimable(self, db):
        """发起时把 last_reminded_at 记成创建时间——否则发起播报之后 30 秒
        又会冒出一条提醒（调度器每 30 秒 tick 一次）。
        """
        team_id, _ = await _create(db)
        assert await db.claim_wish_reminder(team_id, "2026-01-01 00:00:00") is False

    async def test_claim_reminder_only_once_per_window(self, db):
        team_id, _ = await _create(db)
        await db._db.execute(
            "UPDATE wish_teams SET last_reminded_at = '2000-01-01 00:00:00' WHERE id = ?",
            (team_id,),
        )
        await db._db.commit()

        assert await db.claim_wish_reminder(team_id, "2026-01-01 00:00:00") is True
        assert await db.claim_wish_reminder(team_id, "2026-01-01 00:00:00") is False

    async def test_purge_keeps_recent_and_recruiting(self, db):
        old_departed, _ = await _create(db, leader="甲")
        await _fill(db, old_departed, ["乙", "丙"])
        recruiting, _ = await _create(db, leader="丁", name="第二支")

        await db._db.execute(
            "UPDATE wish_teams SET created_at = '2000-01-01 00:00:00' WHERE id = ?",
            (old_departed,),
        )
        await db._db.commit()

        assert await db.purge_old_wish_teams(7) == 1
        assert await db.get_wish_team(old_departed) is None
        assert await db.get_wish_team(recruiting) is not None

    async def test_clear_releases_quota(self, db):
        first, _ = await _create(db, leader="甲")
        await _fill(db, first, ["乙", "丙"])
        assert await db.slot_usage(GROUP, SLOT) == 1

        assert await db.clear_wish_teams(GROUP) == 1
        assert await db.slot_usage(GROUP, SLOT) == 0, "清空应当释放当天名额"
        assert await db.get_wish_team(first) is None

    async def test_list_by_status(self, db):
        departed, _ = await _create(db, leader="甲")
        await _fill(db, departed, ["乙", "丙"])
        recruiting, _ = await _create(db, leader="丁", name="第二支")
        gone, _ = await _create(db, leader="戊", name="第三支", recruiting_limit=99)
        await db.disband_wish_team(gone, GROUP)

        listed = await db.list_wish_teams(GROUP, (WISH_RECRUITING, WISH_DEPARTED, WISH_VOIDED))
        assert [t["team_id"] for t in listed] == [departed, recruiting]
        disbanded = await db.list_wish_teams(GROUP, (WISH_DISBANDED,))
        assert [t["team_id"] for t in disbanded] == [gone]

    async def test_get_by_name_and_active_team(self, db):
        team_id, _ = await _create(db)
        await _fill(db, team_id, ["乙", "丙"])

        by_name = await db.get_wish_team_by_name(GROUP, TEAM_NAME)
        assert by_name["team_id"] == team_id
        assert await db.get_wish_team_by_name(GROUP, "不存在的队") is None

        active = await db.get_player_active_wish_team(GROUP, "name:乙")
        assert active["team_id"] == team_id
        assert await db.get_player_active_wish_team(GROUP, "name:路人") is None


class TestStatusPrimitives:
    async def test_grant_never_shortens_existing(self, db):
        """同名状态更晚时保持更晚（防止 add_status 那种无条件覆盖）。"""
        await db.add_status(GROUP, "name:甲", TEAM_NAME, 9, source=STATUS_SOURCE_WISH)
        await db.commit()

        await db._wish_grant_status(GROUP, "name:甲", TEAM_NAME, "2000-01-01 00:00:00")
        await db.commit()

        assert [s["remaining_days"] for s in await _statuses(db, "甲")] == [9]

    async def test_grant_extends_when_later(self, db):
        await db.add_status(GROUP, "name:甲", TEAM_NAME, 1, source=STATUS_SOURCE_WISH)
        await db.commit()

        await db._wish_grant_status(GROUP, "name:甲", TEAM_NAME, "2099-01-01 00:00:00")
        await db.commit()

        assert [s["expire_at"] for s in await _statuses(db, "甲")] == ["2099-01-01 00:00:00"]

    async def test_grant_keeps_block_actions(self, db):
        """续挂不该顺带清掉阻断项（它属于那条状态本身的语义）。"""
        await db.add_status(GROUP, "name:甲", TEAM_NAME, 5, block_actions="prayer")
        await db.commit()

        await db._wish_grant_status(GROUP, "name:甲", TEAM_NAME, "2099-01-01 00:00:00")
        await db.commit()

        assert [s["block_actions"] for s in await _statuses(db, "甲")] == ["prayer"]

    async def test_revoke_only_touches_wish_source(self, db):
        await db.add_status(GROUP, "name:甲", TEAM_NAME, 5)
        await db.commit()

        assert await db._wish_revoke_status(GROUP, "name:甲", TEAM_NAME) == 0
        assert [s["status_name"] for s in await _statuses(db, "甲")] == [TEAM_NAME]

    async def test_trim_keeps_latest(self, db):
        for name in ["第一", "第二", "第三", "第四"]:
            await db.add_status(GROUP, "name:甲", name, 1, source=STATUS_SOURCE_WISH)
            await db.commit()
        # 到期时间都在同一天附近，用显式到期时间拉开顺序
        for name, days in [("第一", 1), ("第二", 2), ("第三", 3), ("第四", 4)]:
            await db._db.execute(
                "UPDATE player_statuses SET expire_at = datetime('now', ?) "
                "WHERE group_id = ? AND player_id = ? AND status_name = ?",
                (f"+{days} days", GROUP, "name:甲", name),
            )
        await db._db.commit()

        assert await db._wish_trim_statuses(GROUP, "name:甲", 3) == 1
        remaining = sorted(s["status_name"] for s in await _statuses(db, "甲"))
        assert remaining == ["第三", "第二", "第四"], "裁掉的不是最早到期的那条"

    async def test_trim_ignores_god_statuses(self, db):
        await db.add_status(GROUP, "name:甲", "诸神的状态", 1)
        await db.add_status(GROUP, "name:甲", "队伍一", 2, source=STATUS_SOURCE_WISH)
        await db.add_status(GROUP, "name:甲", "队伍二", 3, source=STATUS_SOURCE_WISH)
        await db.commit()

        assert await db._wish_trim_statuses(GROUP, "name:甲", 1) == 1
        remaining = sorted(s["status_name"] for s in await _statuses(db, "甲"))
        assert remaining == ["诸神的状态", "队伍二"]
