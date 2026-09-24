"""
配置热生效：改 WebUI 配置后不重载插件也能立刻生效。

覆盖两处此前会"冻结到重启"的缓存：
- 祷词表 / 指令前缀集合（启动时构建，原先无任何失效路径）
- 权限判定结果（TTL 5 分钟，改 admin_ids/白名单后最长陈旧 5 分钟）
"""

import pytest

from astrbot_plugin_faith_ladder.commands.config import ConfigMixin
from astrbot_plugin_faith_ladder.commands.prayer import PrayerCommandsMixin
from astrbot_plugin_faith_ladder.plugin_config import config_snapshot


class _PrayerHost(ConfigMixin, PrayerCommandsMixin):
    """最小宿主：只需要 config。"""

    def __init__(self, config):
        self.config = config


def _host(**config):
    host = _PrayerHost(dict(config))
    host._build_prayer_cache()
    return host


class TestPrayerCacheHotReload:
    def test_new_prayer_is_picked_up_without_restart(self):
        host = _host(prayer_text_命运=["命若繁星望而不及"])
        assert "命若繁星望而不及" in host._prayer_cache

        # 模拟 WebUI 改配置：config 是同一个 dict 的引用，直接改内容
        host.config["prayer_text_命运"] = ["命若繁星望而不及", "星移斗转命数难违"]

        host._ensure_prayer_cache()
        assert host._prayer_cache["星移斗转命数难违"] == "命运"

    def test_cache_is_not_rebuilt_when_config_unchanged(self):
        host = _host(prayer_text_命运=["命若繁星望而不及"])
        host._prayer_cache["__sentinel__"] = "X"

        host._ensure_prayer_cache()
        assert host._prayer_cache.get("__sentinel__") == "X", "配置没变时不该重建"

    def test_command_prefix_change_takes_effect(self):
        host = _host(cmd_ladder="天梯榜")
        assert host._is_command_message("天梯榜") is True
        assert host._is_command_message("排行榜") is False

        host.config["cmd_ladder"] = "排行榜"
        host._ensure_prayer_cache()

        assert host._is_command_message("排行榜") is True
        assert host._is_command_message("天梯榜") is False, "旧前缀也该随配置一起失效"

    def test_removing_a_prayer_takes_effect(self):
        host = _host(prayer_text_秩序=["秩序井然不可逾越"])
        assert "秩序井然不可逾越" in host._prayer_cache

        host.config["prayer_text_秩序"] = []
        host._ensure_prayer_cache()

        assert "秩序井然不可逾越" not in host._prayer_cache

    def test_snapshot_covers_schema_prayer_and_cmd_keys(self):
        """快照必须覆盖所有参与键：漏键就会出现"改了配置但缓存没重建"。"""
        from astrbot_plugin_faith_ladder.commands.prayer import _prayer_cache_config_keys

        keys = _prayer_cache_config_keys()
        assert sum(1 for k in keys if k.startswith("prayer_text_")) == 16
        assert sum(1 for k in keys if k.startswith("cmd_")) >= 12
        # 快照长度与键数一致
        host = _host()
        assert len(host._prayer_cache_snapshot) == len(keys)


class TestPermissionCacheHotReload:
    async def test_deprecated_config_whitelist_no_longer_grants(self, db_manager):
        """WebUI 的 whitelist 配置已废弃：改它既不授权，也不该刷新权限缓存。"""
        from astrbot_plugin_faith_ladder.permission_service import PermissionService

        config = {"admin_ids": [], "whitelist": []}
        service = PermissionService(db_manager, config_getter=lambda: config)

        assert await service.check_score_permission("10001") is False

        # 这一层已经不再参与判定——权限只认 admin_ids 与 DB 白名单
        config["whitelist"] = [{"type": "user", "id": "10001"}]
        assert await service.check_score_permission("10001") is False

    async def test_admin_ids_change_invalidates_cached_result(self, db_manager):
        from astrbot_plugin_faith_ladder.permission_service import PermissionService

        config = {"admin_ids": [], "whitelist": []}
        service = PermissionService(db_manager, config_getter=lambda: config)

        assert await service.check_score_permission("10002") is False
        config["admin_ids"] = ["10002"]
        assert await service.check_score_permission("10002") is True

    async def test_unchanged_config_still_hits_cache(self, db_manager):
        """配置没变时缓存要继续命中，否则每次指令都打一次 DB。"""
        from astrbot_plugin_faith_ladder.permission_service import PermissionService

        calls = {"n": 0}
        original = db_manager.is_whitelisted

        async def counting(user_id):
            calls["n"] += 1
            return await original(user_id)

        db_manager.is_whitelisted = counting
        config = {"admin_ids": [], "whitelist": []}
        service = PermissionService(db_manager, config_getter=lambda: config)

        await service.check_score_permission("10003")
        await service.check_score_permission("10003")
        assert calls["n"] == 1, "无配置变化时应命中缓存"

    async def test_explicit_invalidate_still_works(self, db_manager):
        """白名单增删路径调用的 invalidate_cache 语义不变。"""
        from astrbot_plugin_faith_ladder.permission_service import PermissionService

        config = {"admin_ids": [], "whitelist": []}
        service = PermissionService(db_manager, config_getter=lambda: config)
        assert await service.check_score_permission("10004") is False

        await db_manager.add_to_whitelist("user", "10004", "tester")
        await db_manager.commit()
        service.invalidate_cache("10004")

        assert await service.check_score_permission("10004") is True


class TestSnapshotHelper:
    def test_snapshot_changes_with_value(self):
        keys = ("admin_ids",)
        assert config_snapshot({}, keys) != config_snapshot({"admin_ids": ["1"]}, keys)

    def test_snapshot_stable_for_same_value(self):
        keys = ("admin_ids", "whitelist")
        a = config_snapshot({"admin_ids": ["1"], "whitelist": [{"id": "2"}]}, keys)
        b = config_snapshot({"admin_ids": ["1"], "whitelist": [{"id": "2"}]}, keys)
        assert a == b

    def test_snapshot_treats_null_as_default(self):
        keys = ("admin_ids",)
        assert config_snapshot({"admin_ids": None}, keys) == config_snapshot({}, keys)
