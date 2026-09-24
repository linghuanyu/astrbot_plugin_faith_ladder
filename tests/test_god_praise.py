"""
诸神赞美：诸神列表里配了信仰的人，发出命中**自己信仰**的祷词时回复「赞美【<信仰>】」。

口径（实现见 commands/prayer.py 的 8.6 段）：
- 严格只认诸神列表（DB 白名单）——超管、群主/群管理员都拿不到
- 只认自己的信仰：无信仰不触发，祈祷别的信仰也不触发
- 不计分、不写 prayer_daily_hits、不限次数（与玩家的「每天一次」无关，玩家流程没动）
"""

import contextlib

from astrbot_plugin_faith_ladder.messages import GOD_PRAISE, feature_disabled_message
from astrbot_plugin_faith_ladder.models import VALID_FAITHS

GROUP = "100"
GOD = "555"      # 诸神列表里的人
ADMIN = "999"    # 超管：不在诸神列表里
PLAYER_QQ = "556"

# 祷词直接用 schema 默认值（恰好 8 个汉字）
PRAYER_诞育 = "感孕众生衔育自然"
PRAYER_湮灭 = "于无中生于寂中灭"

CONFIG = {
    "admin_ids": [ADMIN],
    "prayer_trigger_groups": [GROUP],
    "prayer_text_诞育": [PRAYER_诞育],
    "prayer_text_湮灭": [PRAYER_湮灭],
}


class _Sender:
    def __init__(self):
        self.role = "member"


class _MessageObj:
    def __init__(self, group_id):
        self.sender = _Sender()
        self.group_id = group_id


class _Event:
    """祷词触发用的最小群消息事件（比 test_whitelist_flow 那份多一个 stop_event）。"""

    def __init__(self, sender_id, text, group_id=GROUP):
        self._sender_id = sender_id
        self.message_str = text
        self.message_obj = _MessageObj(group_id)
        self.stopped = False

    def get_sender_id(self):
        return self._sender_id

    def plain_result(self, text):
        return text

    def stop_event(self):
        self.stopped = True


@contextlib.asynccontextmanager
async def _plugin(**overrides):
    import astrbot_plugin_faith_ladder.main as m

    p = m.FaithLadderPlugin(m.Context(), {**CONFIG, **overrides})
    await p.initialize()
    try:
        yield p
    finally:
        await p.terminate()


async def _pray(plugin, event):
    """跑一次祷词监听，返回 (回复列表, event)。"""
    return [r async for r in plugin._prayer_message_impl(event)], event


async def _add_god(plugin, faith=None, qq=GOD):
    await plugin.db_manager.add_to_whitelist("user", qq, ADMIN, faith=faith)


async def _count_daily_hits(plugin):
    async with plugin.db_manager._db.execute("SELECT COUNT(*) FROM prayer_daily_hits") as cur:
        return (await cur.fetchone())[0]


class TestGodPraise:
    async def test_god_praying_own_faith_is_praised(self, stubbed_astrbot):
        async with _plugin() as p:
            await _add_god(p, faith="诞育")
            replies, event = await _pray(p, _Event(GOD, PRAYER_诞育))

            assert replies == [GOD_PRAISE.format(faith="诞育")]
            assert replies[0] == "赞美【诞育】"
            assert event.stopped is True, "不 stop_event 的话 AI 人格还会对同一条消息再回一次"

    async def test_god_praying_another_faith_is_silent(self, stubbed_astrbot):
        """只认自己的信仰：诞育的诸神祈祷湮灭，什么都不回。"""
        async with _plugin() as p:
            await _add_god(p, faith="诞育")
            replies, event = await _pray(p, _Event(GOD, PRAYER_湮灭))

            assert replies == []
            assert event.stopped is False

    async def test_god_without_faith_is_silent(self, stubbed_astrbot):
        """白名单允许留空信仰，这类「普通诸神」不触发。"""
        async with _plugin() as p:
            await _add_god(p, faith=None)
            replies, _ = await _pray(p, _Event(GOD, PRAYER_诞育))

            assert replies == []

    async def test_admin_without_whitelist_row_is_silent(self, stubbed_astrbot):
        """超管不等于诸神：他的信仰只存在诸神列表那一行里，不在列表就没有信仰。"""
        async with _plugin() as p:
            replies, _ = await _pray(p, _Event(ADMIN, PRAYER_诞育))

            assert replies == []

    async def test_unlimited_frequency(self, stubbed_astrbot):
        """不限次数：同一句连着发，每次都回（玩家那条路径是每天一次，两者无关）。"""
        async with _plugin() as p:
            await _add_god(p, faith="诞育")

            first, _ = await _pray(p, _Event(GOD, PRAYER_诞育))
            second, _ = await _pray(p, _Event(GOD, PRAYER_诞育))

            assert first == second == ["赞美【诞育】"]

    async def test_praise_writes_nothing_to_db(self, stubbed_astrbot):
        """开着计分也不动分：祷词日记录一行都不该写（写了就会连带算分/算连续天数）。"""
        async with _plugin(prayer_score_enabled=True) as p:
            await _add_god(p, faith="诞育")
            await _pray(p, _Event(GOD, PRAYER_诞育))

            assert await _count_daily_hits(p) == 0


class TestGateAppliesToGods:
    """赞美在闸门之后，所以诸神一样受群访问控制与功能开关约束。"""

    async def test_feature_disabled_blocks_praise(self, stubbed_astrbot):
        async with _plugin(feature_prayer_enabled=False) as p:
            await _add_god(p, faith="诞育")
            replies, _ = await _pray(p, _Event(GOD, PRAYER_诞育))

            assert replies == [feature_disabled_message("feature_prayer_enabled")]

    async def test_group_not_in_trigger_list_is_silent(self, stubbed_astrbot):
        async with _plugin(prayer_trigger_groups=[]) as p:
            await _add_god(p, faith="诞育")
            replies, _ = await _pray(p, _Event(GOD, PRAYER_诞育))

            assert replies == []


class TestPlayerPathUnchanged:
    async def test_non_god_player_still_gets_prayer_reply(self, stubbed_astrbot):
        """没进诸神列表的普通玩家走原流程，拿到的绝不能是赞美。"""
        async with _plugin() as p:
            await p.db_manager.upsert_player(GROUP, "name:张三", "张三")
            await p.db_manager.set_player_specific_faith(GROUP, "name:张三", "诞育")
            await p.db_manager.set_player_qq(GROUP, "name:张三", PLAYER_QQ)
            await p.db_manager.commit()

            replies, event = await _pray(p, _Event(PLAYER_QQ, PRAYER_诞育))

            assert len(replies) == 1
            assert "赞美【" not in replies[0]
            assert event.stopped is True


class TestPrayerTextGuard:
    """祷词配重复或配空 = 那个信仰静默失效（构建缓存时会 warning）。
    这里钉住 schema 默认值，别再改出一条撞车或空白的祷词。
    """

    def test_all_faiths_have_unique_eight_char_prayer(self):
        from astrbot_plugin_faith_ladder.plugin_config import schema_default
        from astrbot_plugin_faith_ladder.text_utils import PRAYER_NORMALIZE_RE

        seen = {}
        for faith in VALID_FAITHS:
            prayers = schema_default(f"prayer_text_{faith}") or []
            assert prayers, f"{faith} 没有祷词"
            for prayer in prayers:
                normalized = PRAYER_NORMALIZE_RE.sub("", prayer)
                assert len(normalized) == 8, f"{faith} 的祷词「{prayer}」不是 8 个汉字"
                assert normalized not in seen, (
                    f"祷词「{normalized}」同时配给了 {seen.get(normalized)} 与 {faith}，"
                    f"{faith} 会覆盖前者"
                )
                seen[normalized] = faith

    async def test_duplicate_prayer_only_later_faith_triggers(self, stubbed_astrbot):
        """撞车时的实际行为：VALID_FAITHS 里靠后的信仰胜出，靠前的那个静默。"""
        async with _plugin(prayer_text_湮灭=[PRAYER_诞育]) as p:
            await _add_god(p, faith="诞育")
            replies, _ = await _pray(p, _Event(GOD, PRAYER_诞育))

            assert replies == [], "诞育（靠前）被湮灭（靠后）覆盖，不该触发"

    async def test_cache_build_warns_on_collision_and_empty(self, stubbed_astrbot, caplog):
        """静默失效必须留痕：撞车与空祷词在建表时各告警一条，否则没人知道该信仰废了。"""
        import logging

        with caplog.at_level(logging.WARNING):
            async with _plugin(
                prayer_text_湮灭=[PRAYER_诞育],   # 与诞育撞车
                prayer_text_命运=[],              # 配空
            ):
                pass

        logged = [r.getMessage() for r in caplog.records]
        assert any("同时配给了" in m and "诞育" in m and "湮灭" in m for m in logged), logged
        assert any("prayer_text_命运 为空" in m for m in logged), logged
