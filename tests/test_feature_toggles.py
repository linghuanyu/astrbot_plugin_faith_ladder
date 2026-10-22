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


class TestHelpHidesDisabledFeatures:
    """帮助里不展示被关掉的功能：列着用不了的指令只会让人白试一遍。"""

    # 段落标题 → 管辖它的开关（默认值见 _conf_schema.json）
    SECTION_FEATURES = {
        "[排行榜]": "feature_scoreboard_enabled",
        "[赠送道具]": "feature_gift_enabled",
        "[祈愿试炼]": "feature_wish_enabled",
        "[沉浸感]": "wager_enabled",
        "[群管]": "feature_qq_admin_enabled",
    }

    # wager_enabled 默认关闭，所以「沉浸感」段默认就不该出现（与其余几块不同）
    DEFAULT_ON_SECTIONS = {
        "[排行榜]", "[赠送道具]", "[祈愿试炼]", "[群管]",
    }

    def _help(self, **config):
        from astrbot_plugin_faith_ladder.message_formatter import format_help

        return format_help(dict(config))

    def test_default_sections_match_schema_defaults(self):
        """默认开的那几块都要在；默认关的「沉浸感」不出现。"""
        text = self._help()
        for section in self.DEFAULT_ON_SECTIONS:
            assert section in text, f"默认应展示 {section}（对应开关默认开）"
        assert "[沉浸感]" not in text, "wager_enabled 默认关，赌局一行不该列出"

    def test_section_order_is_stable(self):
        """打开全部开关后，段落顺序应与改造前一致。"""
        text = self._help(wager_enabled=True)
        positions = [text.index(section) for section in self.SECTION_FEATURES]
        assert positions == sorted(positions), "段落顺序不应该被过滤逻辑打乱"

    @pytest.mark.parametrize("section,feature_key", sorted(SECTION_FEATURES.items()))
    def test_section_hidden_when_its_feature_is_off(self, section, feature_key):
        text = self._help(**{feature_key: False})
        assert section not in text, f"{feature_key} 关掉后不该再展示 {section}"

    def test_wager_section_appears_when_enabled(self):
        """赌局默认关闭，打开后帮助里才出现这一段。"""
        assert "[沉浸感]" in self._help(wager_enabled=True)

    def test_other_sections_survive_single_toggle(self):
        """只关一块时，其余各段照旧出现（避免过滤逻辑误伤）。"""
        text = self._help(feature_gift_enabled=False, wager_enabled=True)
        for section in self.SECTION_FEATURES:
            if section == "[赠送道具]":
                continue
            assert section in text, f"关掉赠送不该影响 {section}"

    def test_inventory_section_keeps_admin_lines(self):
        """储物空间的查询是玩家指令（受开关管辖），赐予/收回/清除是诸神指令，必须留着。"""
        text = self._help(feature_inventory_enabled=False)
        assert "[储物空间]" in text, "整段不能消失：诸神的道具管理还在"
        assert "查询储物空间" not in text, "玩家查询被关掉后不该再出现"
        for line in ("赐予道具", "收回道具", "清除储物空间"):
            assert line in text, f"{line} 是诸神指令，不受 feature_inventory_enabled 影响"

    def test_header_and_footer_are_always_present(self):
        text = self._help(
            feature_scoreboard_enabled=False,
            feature_gift_enabled=False,
            feature_inventory_enabled=False,
            feature_prayer_enabled=False,
            feature_qq_admin_enabled=False,
            feature_wish_enabled=False,
            wager_enabled=False,
        )
        assert "诸   神  愚   戏" in text
        assert "初始之位" in text
        assert "天梯榜帮助 - 显示本帮助" in text


class TestCheckPlayerRemoved:
    """「检测玩家」已被「信仰注册」取代：实现体、注册与帮助文案都不该再出现。"""

    def test_impl_gone(self):
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1] / "commands" / "player.py").read_text(
            encoding="utf-8"
        )
        assert "_check_player_impl" not in src

    def test_command_not_registered(self):
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        assert '"检测玩家"' not in src

    def test_help_no_longer_lists_it(self):
        from astrbot_plugin_faith_ladder.message_formatter import format_help

        assert "检测玩家" not in format_help({})
