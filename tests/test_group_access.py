"""
群级访问控制（off / blacklist / whitelist）与闸门守卫。

背景：插件此前只能靠"多部署一个实例"来限制服务范围；闸门同时是后面
功能开关与状态阻断的落点，所以这里既测行为，也守"每个入口都必须过闸门"。
"""

import ast
import re
from pathlib import Path

import pytest

from astrbot_plugin_faith_ladder.commands.config import ConfigMixin
from astrbot_plugin_faith_ladder.commands.gate import GateMixin

ROOT = Path(__file__).resolve().parent.parent
COMMANDS_DIR = ROOT / "commands"

# 这些实现体不针对某个群，故不过群访问闸门
GATE_EXEMPT = {"_help_impl", "_whitelist_impl", "_sync_whitelist_impl", "_group_member_change_impl"}


class _Host(ConfigMixin, GateMixin):
    def __init__(self, config, group_id="100"):
        self.config = config
        self._group_id = group_id

    def _get_group_id(self, event):
        return self._group_id


class _Event:
    def get_sender_id(self):
        return "999"


def _host(**config):
    host = _Host(dict(config))
    return host


class TestGroupAccessDecision:
    async def test_off_allows_everything(self):
        host = _host(group_access_mode="off", group_access_list=["100"])
        assert await host._gate(_Event()) == (False, None)

    async def test_blacklist_blocks_listed_group_silently(self):
        host = _host(group_access_mode="blacklist", group_access_list=["100"])
        assert await host._gate(_Event()) == (True, None), "被拦截时不带文案 = 静默"

    async def test_blacklist_allows_other_groups(self):
        host = _host(group_access_mode="blacklist", group_access_list=["200"])
        assert await host._gate(_Event()) == (False, None)

    async def test_whitelist_allows_listed_group(self):
        host = _host(group_access_mode="whitelist", group_access_list=["100"])
        assert await host._gate(_Event()) == (False, None)

    async def test_whitelist_blocks_other_groups(self):
        host = _host(group_access_mode="whitelist", group_access_list=["200"])
        assert await host._gate(_Event()) == (True, None)

    async def test_whitelist_with_empty_list_blocks_everything(self):
        host = _host(group_access_mode="whitelist", group_access_list=[])
        assert await host._gate(_Event()) == (True, None)

    async def test_unknown_mode_falls_back_to_off(self):
        """配置写错（例如手改 JSON 写成 wl）时放行：宁可不生效，也不要因为笔误把功能全关掉。"""
        host = _host(group_access_mode="wl", group_access_list=["100"])
        assert await host._gate(_Event()) == (False, None)

    async def test_string_list_is_tolerated(self):
        """WebUI 把列表存成字符串时也要能判断。"""
        host = _host(group_access_mode="blacklist", group_access_list="100,200")
        assert await host._gate(_Event()) == (True, None)

    async def test_group_match_is_exact(self):
        host = _host(group_access_mode="blacklist", group_access_list=["1000"])
        assert await host._gate(_Event()) == (False, None), "100 不应匹配 1000"


class TestGateOnRealCommand:
    """跑一个真实实现体，确认被拦时既不回复也不调用服务层。"""

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
        def __init__(self, config, group_id):
            self.config = config
            self._group_id = group_id
            self.ladder_service = TestGateOnRealCommand._Service()
            self.cooldown_manager = TestGateOnRealCommand._Cooldown()

        async def _check_perm(self, event):
            return True

        def _get_group_id(self, event):
            return self._group_id

        async def _send_forward_text(self, event, group_id, title, text):
            return True

    async def _run(self, config, group_id):
        from astrbot_plugin_faith_ladder.commands.scoreboard import ScoreboardCommandsMixin

        host = self._LadderHost(config, group_id)
        replies = [r async for r in ScoreboardCommandsMixin._ladder_impl(host, self._CommandEvent())]
        return host, replies

    async def test_blacklisted_group_gets_silence_and_no_service_call(self):
        host, replies = await self._run(
            {"group_access_mode": "blacklist", "group_access_list": ["100"]}, "100"
        )
        assert replies == [], "被拦截的群不应收到任何回复"
        assert host.ladder_service.calls == [], "被拦截时不应触碰服务层/数据库"

    async def test_whitelisted_group_runs_normally(self):
        host, _ = await self._run(
            {"group_access_mode": "whitelist", "group_access_list": ["100"]}, "100"
        )
        assert host.ladder_service.calls == ["100"]


class TestGateCoverageGuards:
    """静态守卫：新增指令时漏加闸门会在这里失败。"""

    def _impls(self):
        for path in sorted(COMMANDS_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.AsyncFunctionDef) and node.name.endswith("_impl"):
                    yield path, node

    def test_every_impl_goes_through_gate(self):
        missing = []
        for path, node in self._impls():
            if node.name in GATE_EXEMPT:
                continue
            src = ast.unparse(node)
            if "_gate(" not in src:
                missing.append(f"{path.name}:{node.lineno} {node.name}")
        assert missing == [], "这些实现体没有过闸门（新增指令请调用 self._gate）：\n" + "\n".join(missing)

    def test_gate_is_the_first_statement(self):
        """闸门必须挡在第一条回复/数据库操作之前。"""
        offenders = []
        for path, node in self._impls():
            if node.name in GATE_EXEMPT:
                continue
            gate_line = None
            first_effect_line = None
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    src = ast.unparse(sub)
                    if "_gate(" in src and gate_line is None:
                        gate_line = sub.lineno
                if isinstance(sub, (ast.Yield, ast.YieldFrom)) and first_effect_line is None:
                    first_effect_line = sub.lineno
            if gate_line is None:
                continue  # 由上一个用例报错
            if first_effect_line is not None and gate_line > first_effect_line:
                offenders.append(f"{path.name}:{node.lineno} {node.name}（闸门在第 {gate_line} 行，首次产出在第 {first_effect_line} 行）")
        assert offenders == [], "闸门被放在回复之后，等于没拦：\n" + "\n".join(offenders)

    def test_exempt_impls_are_the_expected_ones(self):
        """豁免名单一旦变大就会在这里被发现（避免有人顺手绕过闸门）。"""
        all_impls = {node.name for _, node in self._impls()}
        without_gate = set()
        for path, node in self._impls():
            if "_gate(" not in ast.unparse(node):
                without_gate.add(node.name)
        assert without_gate <= GATE_EXEMPT, f"出现了计划外的无闸门实现体: {sorted(without_gate - GATE_EXEMPT)}"

    def test_group_member_change_respects_group_access(self):
        """白名单自动同步（事件监听，不走闸门）也要跳过未启用的群。"""
        src = (COMMANDS_DIR / "admin.py").read_text(encoding="utf-8")
        assert "_group_access_blocked(group_id)" in src


def test_qq_admin_handlers_go_through_preflight():
    """8 个群管 handler 每个都要先过 _preflight（它们在 main.py 里直接调用，不经 mixin）。"""
    path = ROOT / "qq_admin_handle.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    handlers = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name.startswith("handle_")
    ]
    assert len(handlers) == 8, f"群管 handler 数量变了（{len(handlers)}），请同步更新守卫"
    missing = [f"{n.name}:{n.lineno}" for n in handlers if "_preflight(" not in ast.unparse(n)]
    assert missing == [], "这些群管 handler 没有过闸门：\n" + "\n".join(missing)
