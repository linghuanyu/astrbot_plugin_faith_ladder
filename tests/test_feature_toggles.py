"""
模块级功能开关：关掉某个玩法时给出明确提示，且不产生任何数据变化。

开关判定收在 commands/gate.py::GateMixin._gate 里（与群访问控制、状态阻断同一处），
所以这里既测判定，也测"关掉后真的什么都没发生"。
"""

import pytest

from astrbot_plugin_faith_ladder.commands.gate import ACTION_FEATURE
from astrbot_plugin_faith_ladder.commands.config import ConfigMixin
from astrbot_plugin_faith_ladder.commands.gate import GateMixin
from astrbot_plugin_faith_ladder.messages import FEATURE_LABELS, feature_disabled_message
from astrbot_plugin_faith_ladder.plugin_config import schema_has


class _Host(ConfigMixin, GateMixin):
    def __init__(self, config):
        self.config = config

    def _get_group_id(self, event):
        return "100"


class _Event:
    def get_sender_id(self):
        return "999"


def _host(**config):
    return _Host(dict(config))


class TestFeatureDecision:
    @pytest.mark.parametrize("action", sorted(ACTION_FEATURE))
    async def test_action_is_blocked_when_its_feature_is_off(self, action):
        host = _host(**{ACTION_FEATURE[action]: False})
        blocked, msg = await host._gate(_Event(), action)
        assert blocked is True
        assert msg == feature_disabled_message(ACTION_FEATURE[action])

    @pytest.mark.parametrize("action", sorted(ACTION_FEATURE))
    async def test_action_is_allowed_by_default(self, action):
        """默认全开：不写任何开关时行为与改造前一致。"""
        host = _host()
        assert await host._gate(_Event(), action) == (False, None)

    async def test_unrelated_action_is_not_affected(self):
        """关掉赠送不影响录入积分（None 动作不查开关）。"""
        host = _host(feature_gift_enabled=False)
        assert await host._gate(_Event(), None) == (False, None)

    async def test_storage_actions_are_not_gated_by_design(self):
        """管理类动作（赐予/收回/录入等）传 None，不受开关影响，避免管理员把自己锁死。"""
        host = _host(
            feature_gift_enabled=False, feature_inventory_enabled=False,
            feature_scoreboard_enabled=False, feature_prayer_enabled=False,
        )
        assert await host._gate(_Event(), None) == (False, None)


class TestFeatureTableIntegrity:
    def test_every_feature_has_label_and_schema_key(self):
        for action, feature_key in ACTION_FEATURE.items():
            assert schema_has(feature_key), f"{action} 指向的 {feature_key} 不在 schema 里"
            assert feature_key in FEATURE_LABELS, f"{feature_key} 没有给玩家看的名字"

    def test_disabled_message_names_the_feature(self):
        assert "道具赠送" in feature_disabled_message("feature_gift_enabled")
        assert "群管指令" in feature_disabled_message("feature_qq_admin_enabled")


class TestFeatureToggleOnRealCommand:
    """跑真实实现体：关掉排行榜后既不回复榜单也不碰服务层。"""

    class _Service:
        def __init__(self):
            self.calls = []

        async def get_leaderboard_text(self, group_id, limit, min_ladder_score):
            self.calls.append(group_id)
            return "榜单"

    class _Cooldown:
        def check_cooldown(self, key, seconds):
            return True

        def set_cooldown(self, key):
            pass

    class _CommandEvent:
        def get_sender_id(self):
            return "999"

        def plain_result(self, text):
            return text

        def stop_event(self):
            pass

    class _LadderHost(ConfigMixin, GateMixin):
        def __init__(self, config):
            self.config = config
            self.ladder_service = TestFeatureToggleOnRealCommand._Service()
            self.cooldown_manager = TestFeatureToggleOnRealCommand._Cooldown()

        async def _check_perm(self, event):
            return True

        def _get_group_id(self, event):
            return "100"

        async def _send_forward_text(self, event, group_id, title, text):
            return True

    async def _run(self, **config):
        from astrbot_plugin_faith_ladder.commands.scoreboard import ScoreboardCommandsMixin

        host = self._LadderHost(dict(config))
        replies = [r async for r in ScoreboardCommandsMixin._ladder_impl(host, self._CommandEvent())]
        return host, replies

    async def test_disabled_scoreboard_replies_and_skips_service(self):
        host, replies = await self._run(feature_scoreboard_enabled=False)
        assert replies == ["「排行榜」功能已被管理员关闭。"]
        assert host.ladder_service.calls == []

    async def test_enabled_scoreboard_runs(self):
        host, _ = await self._run()
        assert host.ladder_service.calls == ["100"]
