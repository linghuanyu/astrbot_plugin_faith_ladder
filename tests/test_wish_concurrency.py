"""
祈愿试炼的并发不变式测试。

单连接 SQLite 上「并发」的真实形态是：每个对外方法被 `_write_guard` 串行化，
所以并发调用不会交错改写，但**顺序不确定**。这里钉的就是「无论谁先谁后，
这些不变量都必须成立」：

- 名额只认领一次（并发发车只有一支成功）。
- 容量不被突破（并发加入不会超员）。
- 状态只挂一次、且与队伍到期时间一致。
- 两项每日上限不会被并发突破。
- 一个提醒窗口内只有一次提醒被认领。
"""

import asyncio

import pytest
import pytest_asyncio

from astrbot_plugin_faith_ladder.db_manager import (
    WISH_DEPARTED,
    WISH_RECRUITING,
    WISH_VOIDED,
    DatabaseManager,
)

GROUP = "g1"
SLOT = "2026-09-28"
CREATE_DAY = "2026-09-25"
NAME = "09月28日祈愿试炼"


@pytest_asyncio.fixture
async def db(temp_data_dir):
    dbm = DatabaseManager(temp_data_dir)
    await dbm.initialize()
    yield dbm
    await dbm.close()


async def _create(db, leader, name=NAME, capacity=3, recruiting_limit=9):
    return await db.create_wish_team(
        GROUP, name, capacity, f"name:{leader}", leader, SLOT, CREATE_DAY,
        recruiting_limit,
    )


async def _join(db, team_id, who, slot_limit=1, capacity=3):
    return await db.join_wish_team(
        team_id, GROUP, f"name:{who}", who, slot_limit=slot_limit,
        status_days=3, keep_statuses=3,
    )


async def _roster(db, team_id):
    return sorted(m["player_name"] for m in (await db.get_wish_team(team_id))["members"])


async def _wish_status_names(db, who):
    return [s["status_name"] for s in await db.get_player_statuses(GROUP, f"name:{who}")]


class TestSlotClaim:
    async def test_concurrent_departures_claim_slot_once(self, db):
        """两支队同时凑满、名额只有 1 个 → 只有一支发车。"""
        first, _ = await _create(db, "甲")
        second, _ = await _create(db, "丁", name="第二支")
        await _join(db, first, "乙")
        await _join(db, second, "戊")

        results = await asyncio.gather(
            _join(db, first, "丙"),
            _join(db, second, "己"),
        )
        codes = sorted(code for code, _ in results)
        assert codes == ["departed", "no_slot"], f"实际得到 {codes}"

        assert await db.slot_usage(GROUP, SLOT) == 1
        teams = await db.list_wish_teams(GROUP, (WISH_DEPARTED,))
        assert len(teams) == 1

        # 状态只挂一次：输的那支队一个人都不该拿到
        departed = teams[0]
        winners = {m["player_name"] for m in departed["members"]}
        losers = {"甲", "乙", "丙", "丁", "戊", "己"} - winners
        for who in winners:
            assert await _wish_status_names(db, who) == [departed["name"]]
        for who in losers:
            assert await _wish_status_names(db, who) == []

    async def test_void_siblings_after_race(self, db):
        """抢输的那支队由 void_sibling_teams 宣判，不留在招募中干等超时。"""
        first, _ = await _create(db, "甲")
        second, _ = await _create(db, "丁", name="第二支")
        await _join(db, first, "乙")
        await _join(db, second, "戊")
        await asyncio.gather(_join(db, first, "丙"), _join(db, second, "己"))

        voided = await db.void_sibling_teams(GROUP, SLOT)
        assert [t["team_id"] for t in voided] == [second]
        assert (await db.get_wish_team(second))["status"] == WISH_VOIDED


class TestCapacity:
    async def test_concurrent_joins_never_overfill(self, db):
        """容量 2 的队伍三个人同时抢 → 只进两个，且只发一次车。"""
        team_id, _ = await _create(db, "甲", capacity=2)

        results = await asyncio.gather(
            _join(db, team_id, "乙", capacity=2),
            _join(db, team_id, "丙", capacity=2),
            _join(db, team_id, "丁", capacity=2),
        )
        codes = sorted(code for code, _ in results)
        assert codes == ["departed", "full", "full"], f"实际得到 {codes}"

        team = await db.get_wish_team(team_id)
        assert len(team["members"]) == 2
        assert await db.slot_usage(GROUP, SLOT) == 1
        # 三人各挂一次状态的条件：只有进队的两人有
        members = {m["player_name"] for m in team["members"]}
        assert members == {"甲", "乙"} or members == {"甲", "丙"} or members == {"甲", "丁"}

    async def test_concurrent_same_player_joins_once(self, db):
        team_id, _ = await _create(db, "甲", capacity=3)

        results = await asyncio.gather(
            _join(db, team_id, "乙"), _join(db, team_id, "乙"), _join(db, team_id, "乙"),
        )
        assert [code for code, _ in results].count("joined") == 1
        assert [code for code, _ in results].count("already_member") == 2
        assert await _roster(db, team_id) == sorted(["甲", "乙"])


class TestConcurrentCreates:
    async def test_concurrent_creates_never_exceed_recruiting_limit(self, db):
        """并发发起撑不破「同时招募中」的上限（这是现在唯一的前置闸门）。

        v3.9.2 起没有「按次数」的配额了，所以这里断言的是并发下数量不超标，
        而不是「第 N 次被拒」。
        """
        leaders = ["甲", "乙", "丙", "丁", "戊"]
        results = await asyncio.gather(
            *[_create(db, who, name=f"{who}的队", recruiting_limit=2) for who in leaders]
        )
        codes = [code for _, code in results]
        assert codes.count("ok") == 2, f"同时招募中的上限被突破：{codes}"
        assert codes.count("too_many_teams") == 3
        assert len(await db.get_open_wish_teams(GROUP)) == 2


class TestRosterRace:
    async def test_swap_and_remove_do_not_drift(self, db):
        """换人（乙→丁）与移出（丙）并发：名单与状态都不该漂。"""
        team_id, _ = await _create(db, "甲")
        await _join(db, team_id, "乙")
        await _join(db, team_id, "丙")
        expiry = (await db.get_wish_team(team_id))["expire_at"]

        await asyncio.gather(
            db.swap_wish_team_member(team_id, GROUP, "name:乙", "name:丁", "丁", 3),
            db.remove_wish_team_member(team_id, GROUP, "name:丙"),
        )

        roster = await _roster(db, team_id)
        assert roster == ["丁", "甲"], f"实际名单 {roster}"
        assert await _wish_status_names(db, "丁") == [NAME]
        for gone in ["乙", "丙"]:
            assert await _wish_status_names(db, gone) == []
        statuses = await db.get_player_statuses(GROUP, "name:甲")
        assert [s["expire_at"] for s in statuses] == [expiry]
        assert [s["expire_at"] for s in await db.get_player_statuses(GROUP, "name:丁")] == [expiry]


class TestReminder:
    async def test_only_one_reminder_claim_wins(self, db):
        team_id, _ = await _create(db, "甲")
        # 发起时 last_reminded_at = 创建时间，所以先把提醒窗口推成「早就到点」
        await db._db.execute(
            "UPDATE wish_teams SET last_reminded_at = '2000-01-01 00:00:00' WHERE id = ?",
            (team_id,),
        )
        await db._db.commit()

        results = await asyncio.gather(
            *[db.claim_wish_reminder(team_id, "2026-01-01 00:00:00") for _ in range(4)]
        )
        assert results.count(True) == 1

    async def test_reminder_skips_non_recruiting(self, db):
        team_id, _ = await _create(db, "甲")
        await _join(db, team_id, "乙")
        await _join(db, team_id, "丙")

        assert (await db.get_wish_team(team_id))["status"] == WISH_DEPARTED
        assert await db.claim_wish_reminder(team_id, "2026-01-01 00:00:00") is False


class TestStaleSweep:
    async def test_concurrent_sweeps_disband_once(self, db):
        team_id, _ = await _create(db, "甲")
        await db._db.execute(
            "UPDATE wish_teams SET created_at = '2000-01-01 00:00:00' WHERE id = ?", (team_id,)
        )
        await db._db.commit()

        first, second = await asyncio.gather(
            db.disband_stale_wish_teams("2026-01-01 00:00:00"),
            db.disband_stale_wish_teams("2026-01-01 00:00:00"),
        )
        total = len(first) + len(second)
        assert total == 1, "同一支队被两次清扫重复处理"
        assert (await db.get_wish_team(team_id))["status"] != WISH_RECRUITING
