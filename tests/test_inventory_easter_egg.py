"""
储物空间彩蛋的「持续窗口」：命中后一段时间内重复查询显示同一条文案。

分两层测：
- 数据层：窗口的写入、读取、过期与隔离。不 sleep——`seconds <= 0` 写出的行
  一落库即过期（读侧判定是严格大于），据此构造"已过期"这个状态。
- 命令层：跑真实的 `_query_inventory_impl`，验证首次命中落窗口、窗口内不再掷骰、
  过期后回到真实道具，以及三个边界（开关关、文案池空、hold=0）与诸神不受影响。
"""

import pytest

from astrbot_plugin_faith_ladder.commands import query as query_module
from astrbot_plugin_faith_ladder.commands.config import ConfigMixin
from astrbot_plugin_faith_ladder.commands.gate import GateMixin
from astrbot_plugin_faith_ladder.commands.query import QueryCommandsMixin
from astrbot_plugin_faith_ladder.models import Player

EGG = "测试彩蛋文案"


@pytest.mark.asyncio
class TestEasterEggWindow:
    """数据层：一行一玩家，过期靠读时比较。"""

    async def test_window_is_readable_before_expiry(self, db_manager):
        await db_manager.set_inventory_easter_egg("g1", "u1", EGG, 60)
        assert await db_manager.get_active_inventory_easter_egg("g1", "u1") == EGG

    async def test_expired_window_reads_none(self, db_manager):
        await db_manager.set_inventory_easter_egg("g1", "u1", EGG, 0)
        assert await db_manager.get_active_inventory_easter_egg("g1", "u1") is None

    async def test_no_window_reads_none(self, db_manager):
        assert await db_manager.get_active_inventory_easter_egg("g1", "nobody") is None

    async def test_retrigger_overwrites_message(self, db_manager):
        """主键是 (group_id, player_id)：重新触发即覆盖，只留最后一条。"""
        await db_manager.set_inventory_easter_egg("g1", "u1", "第一条", 60)
        await db_manager.set_inventory_easter_egg("g1", "u1", "第二条", 60)
        assert await db_manager.get_active_inventory_easter_egg("g1", "u1") == "第二条"

    async def test_windows_are_scoped_by_group_and_player(self, db_manager):
        await db_manager.set_inventory_easter_egg("g1", "u1", EGG, 60)
        assert await db_manager.get_active_inventory_easter_egg("g2", "u1") is None
        assert await db_manager.get_active_inventory_easter_egg("g1", "u2") is None

    async def test_window_survives_reopen(self, temp_data_dir):
        """窗口落库（而不是内存），所以插件重启后仍然有效——这是选表的理由。"""
        from astrbot_plugin_faith_ladder.db_manager import DatabaseManager

        first = DatabaseManager(temp_data_dir)
        await first.initialize()
        await first.set_inventory_easter_egg("g1", "u1", EGG, 60)
        await first.close()

        second = DatabaseManager(temp_data_dir)
        await second.initialize()
        try:
            assert await second.get_active_inventory_easter_egg("g1", "u1") == EGG
        finally:
            await second.close()


class _FixedRandom:
    """替身 random：掷骰结果固定，并记录被问了几次。

    `rolls` 是"窗口内不再掷骰"的正面证据——第二次查询必须一次都没问过它。
    """

    def __init__(self, roll: float):
        self.roll = roll
        self.rolls = 0

    def random(self):
        self.rolls += 1
        return self.roll

    def choice(self, seq):
        return seq[0]


class _Service:
    """替身 ladder_service：返回可识别的"真实储物空间"，并记录被查了谁。"""

    def __init__(self):
        self.calls = []

    async def get_inventory_text(self, group_id, player_name):
        self.calls.append((group_id, player_name))
        return f"═══ {player_name} 的储物空间 ═══"


class _Event:
    def __init__(self, sender_id="999"):
        self._sender_id = sender_id
        self.stopped = False

    def get_sender_id(self):
        return self._sender_id

    def plain_result(self, text):
        return text

    def stop_event(self):
        self.stopped = True


class _Host(ConfigMixin, GateMixin, QueryCommandsMixin):
    """最小宿主：只实现 `_query_inventory_impl` 走到的那些钩子。"""

    def __init__(self, db_manager, config, has_perm=False, args=None):
        self.config = config
        self.db_manager = db_manager
        self.ladder_service = _Service()
        self._has_perm = has_perm
        self._args = args

    async def _check_perm(self, event):
        return self._has_perm

    async def _resolve_self_player(self, event):
        return Player(player_id="u1", group_id="100", player_name="Alice")

    def _get_group_id(self, event):
        return "100"

    def _get_args(self, event, name):
        return self._args

    async def _send_forward_text(self, event, group_id, title, text):
        return False  # 让实现体回落到纯文本，便于断言


def _cfg(**overrides):
    config = {
        "inventory_easter_egg_enabled": True,
        "inventory_easter_egg_probability": 0.5,
        "inventory_easter_egg_messages": [EGG],
        "inventory_easter_egg_hold_seconds": 60,
    }
    config.update(overrides)
    return config


async def _run(host):
    return [r async for r in QueryCommandsMixin._query_inventory_impl(host, _Event())]


REAL = "═══ Alice 的储物空间 ═══"


@pytest.mark.asyncio
class TestEasterEggOnRealCommand:
    """命令层：跑真实实现体。"""

    async def test_hit_replies_egg_and_opens_window(self, db_manager, monkeypatch):
        monkeypatch.setattr(query_module, "random", _FixedRandom(0.0))
        host = _Host(db_manager, _cfg())

        assert await _run(host) == [EGG]
        # 窗口落库了，之后不掷骰也能取到同一条
        assert await db_manager.get_active_inventory_easter_egg("100", "u1") == EGG
        assert host.ladder_service.calls == []

    async def test_window_replays_without_rolling_again(self, db_manager, monkeypatch):
        fake = _FixedRandom(0.0)
        monkeypatch.setattr(query_module, "random", fake)
        host = _Host(db_manager, _cfg())
        assert await _run(host) == [EGG]

        # 掷骰结果改成必不中：若窗口内还掷骰，这一次就会看到真道具
        fake.roll = 0.99
        assert await _run(host) == [EGG]
        assert fake.rolls == 1, "窗口内不该再消费随机数"
        assert host.ladder_service.calls == []

    async def test_window_expiry_returns_real_inventory(self, db_manager, monkeypatch):
        monkeypatch.setattr(query_module, "random", _FixedRandom(0.99))
        await db_manager.set_inventory_easter_egg("100", "u1", EGG, 0)  # 已过期

        host = _Host(db_manager, _cfg())
        assert await _run(host) == [REAL]
        assert host.ladder_service.calls == [("100", "Alice")]

    async def test_disabled_skips_easter_egg(self, db_manager, monkeypatch):
        monkeypatch.setattr(query_module, "random", _FixedRandom(0.0))
        host = _Host(db_manager, _cfg(inventory_easter_egg_enabled=False))
        assert await _run(host) == [REAL]

    async def test_empty_message_pool_is_silently_skipped(self, db_manager, monkeypatch):
        monkeypatch.setattr(query_module, "random", _FixedRandom(0.0))
        host = _Host(db_manager, _cfg(inventory_easter_egg_messages=[]))
        assert await _run(host) == [REAL]

    async def test_zero_hold_does_not_open_window(self, db_manager, monkeypatch):
        """hold=0 退回老行为：只有命中的那一次显示，不落窗口。"""
        monkeypatch.setattr(query_module, "random", _FixedRandom(0.0))
        host = _Host(db_manager, _cfg(inventory_easter_egg_hold_seconds=0))
        assert await _run(host) == [EGG]
        assert await db_manager.get_active_inventory_easter_egg("100", "u1") is None

        monkeypatch.setattr(query_module, "random", _FixedRandom(0.99))
        assert await _run(host) == [REAL]

    async def test_zero_hold_ignores_existing_window(self, db_manager, monkeypatch):
        """hold=0 时旧窗口立刻失效，不必等它自然到期。"""
        monkeypatch.setattr(query_module, "random", _FixedRandom(0.99))
        await db_manager.set_inventory_easter_egg("100", "u1", EGG, 60)

        host = _Host(db_manager, _cfg(inventory_easter_egg_hold_seconds=0))
        assert await _run(host) == [REAL]

    async def test_hold_default_comes_from_schema(self, db_manager, monkeypatch):
        """不写 hold 配置时走 schema 默认值——默认值只该写在 _conf_schema.json 一处。"""
        monkeypatch.setattr(query_module, "random", _FixedRandom(0.0))
        config = _cfg()
        del config["inventory_easter_egg_hold_seconds"]

        host = _Host(db_manager, config)
        assert await _run(host) == [EGG]
        assert await db_manager.get_active_inventory_easter_egg("100", "u1") == EGG

    async def test_gods_see_through_the_window(self, db_manager, monkeypatch):
        """诸神按名字查该玩家时不受窗口影响——那是给本人的玩笑，不是给诸神的。"""
        monkeypatch.setattr(query_module, "random", _FixedRandom(0.99))
        await db_manager.set_inventory_easter_egg("100", "u1", EGG, 60)

        host = _Host(db_manager, _cfg(), has_perm=True, args="Alice")
        assert await _run(host) == [REAL]
