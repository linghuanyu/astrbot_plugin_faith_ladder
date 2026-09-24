"""
配置读取层（plugin_config.py / ConfigMixin._cfg）的测试与两条静态守卫。

背景：此前 60 处 `self.config.get(key, default)` 各写各的默认值，已经出现
"帮助里 query_cooldown_seconds 显示 600、实际生效 5"这类不一致。现在默认值以
`_conf_schema.json` 为唯一事实来源，本文件锁住这条约定。
"""

import json
import re
from pathlib import Path

import pytest

from astrbot_plugin_faith_ladder.plugin_config import (
    SCHEMA_PATH,
    cfg_get,
    schema_default,
    schema_has,
    schema_keys,
)

ROOT = Path(__file__).resolve().parent.parent
PRODUCTION_FILES = (
    [ROOT / "main.py"]
    + sorted((ROOT / "commands").glob("*.py"))
    + [ROOT / name for name in (
        "message_formatter.py", "permission_service.py", "ladder_service.py",
        "scheduler_service.py", "db_manager.py", "plugin_config.py",
    )]
)


class TestSchemaLoads:
    def test_schema_is_valid_json(self):
        data = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        assert len(data) >= 68

    def test_every_key_has_description_hint_and_type(self):
        data = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        missing = [
            k for k, v in data.items()
            if not isinstance(v, dict) or not v.get("description") or not v.get("hint") or not v.get("type")
        ]
        assert missing == [], f"这些配置项缺少 description/hint/type: {missing}"

    def test_schema_keys_helper(self):
        assert "init_ladder_score" in schema_keys()
        assert schema_has("query_cooldown_seconds") is True
        assert schema_has("不存在的键") is False


class TestCfgGetDefaults:
    def test_defaults_come_from_schema(self):
        assert cfg_get({}, "query_cooldown_seconds") == 5
        assert cfg_get({}, "init_ladder_score") == 1000
        assert cfg_get({}, "leaderboard_min_ladder_score") == 1100
        assert cfg_get({}, "auto_backup_enabled") is True
        assert cfg_get({}, "admin_ids") == []
        assert cfg_get({}, "gift_daily_accept_limit") == 1

    def test_none_config_falls_back_to_defaults(self):
        assert cfg_get(None, "init_ladder_score") == 1000

    def test_null_value_falls_back_to_default(self):
        """WebUI 存了 null 时按默认值处理，而不是把 None 传进业务代码。"""
        assert cfg_get({"query_cooldown_seconds": None}, "query_cooldown_seconds") == 5
        assert cfg_get({"admin_ids": None}, "admin_ids") == []

    def test_list_default_is_copied(self):
        """默认值是可变对象时必须是副本，否则调用方一改就污染全局。"""
        first = cfg_get({}, "admin_ids")
        first.append("123")
        assert cfg_get({}, "admin_ids") == []
        assert schema_default("admin_ids") == []


class TestCfgGetCasting:
    def test_int_from_strings_and_floats(self):
        assert cfg_get({"query_cooldown_seconds": "7"}, "query_cooldown_seconds") == 7
        assert cfg_get({"query_cooldown_seconds": "7.0"}, "query_cooldown_seconds") == 7
        assert cfg_get({"query_cooldown_seconds": 7.9}, "query_cooldown_seconds") == 7

    def test_bad_int_falls_back(self):
        assert cfg_get({"query_cooldown_seconds": "abc"}, "query_cooldown_seconds") == 5

    def test_bool_forms(self):
        for raw in ("true", "1", "yes", "on", "是", "开", True):
            assert cfg_get({"auto_backup_enabled": raw}, "auto_backup_enabled") is True
        for raw in ("false", "0", "no", "off", "", False):
            assert cfg_get({"auto_backup_enabled": raw}, "auto_backup_enabled") is False

    def test_bad_bool_falls_back(self):
        assert cfg_get({"auto_backup_enabled": "也许"}, "auto_backup_enabled") is True

    def test_float_key(self):
        assert cfg_get({"inventory_easter_egg_probability": "0.5"}, "inventory_easter_egg_probability") == 0.5
        assert cfg_get({"inventory_easter_egg_probability": "x"}, "inventory_easter_egg_probability") == 0.05

    def test_list_from_string_is_split(self):
        assert cfg_get({"prayer_trigger_groups": "111,222"}, "prayer_trigger_groups") == ["111", "222"]
        assert cfg_get({"prayer_trigger_groups": ""}, "prayer_trigger_groups") == []

    def test_list_keeps_tuple_and_list(self):
        assert cfg_get({"prayer_trigger_groups": ("1", "2")}, "prayer_trigger_groups") == ["1", "2"]

    def test_string_key_coerces_to_str(self):
        assert cfg_get({"cmd_ladder": 123}, "cmd_ladder") == "123"


class TestCfgGetUnknownKeys:
    def test_unknown_key_returns_caller_default(self):
        assert cfg_get({}, "prayer_trigger_messages_positive_欺诈", None) is None
        assert cfg_get({}, "不存在的键", "兜底") == "兜底"

    def test_unknown_key_reads_through(self):
        """动态拼出的键（如按信仰的文案覆盖）不在 schema 里，但仍要能读到配置值。"""
        config = {"prayer_trigger_messages_positive_欺诈": ["x"]}
        assert cfg_get(config, "prayer_trigger_messages_positive_欺诈") == ["x"]


class TestHelpUsesSchemaDefaults:
    """帮助文案必须显示真实生效的值。

    此前 format_help 里 query_cooldown_seconds 写死 600，而实际生效 5。
    """

    def test_query_cooldown_matches_schema(self):
        from astrbot_plugin_faith_ladder.message_formatter import format_help

        text = format_help({})
        # 查询那一行显示 schema 默认 5 秒，而不是曾经写死的 600
        query_line = next(line for line in text.splitlines() if line.startswith("查询 "))
        assert "冷却 5s" in query_line

    def test_configured_values_still_shown(self):
        from astrbot_plugin_faith_ladder.message_formatter import format_help

        text = format_help({"query_cooldown_seconds": 42, "init_ladder_score": 777})
        assert "42" in text
        assert "登神之路 777" in text


class TestNoBareConfigGet:
    """静态守卫：生产代码不得再直接 config.get，必须走 cfg_get / self._cfg。"""

    PATTERN = re.compile(r"(?:self\.config|self\._config|(?<![\w.])config)\.get\(")

    def test_no_direct_config_get_in_production(self):
        offenders = []
        for path in PRODUCTION_FILES:
            if path.name == "plugin_config.py":  # 读取层内部实现，白名单
                continue
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if self.PATTERN.search(line) and "cfg_get" not in line:
                    offenders.append(f"{path.relative_to(ROOT)}:{lineno} {line.strip()}")
        assert offenders == [], "这些地方还在直接读配置，应改用 self._cfg(...)：\n" + "\n".join(offenders)


class TestConfigKeysExistInSchema:
    """静态守卫：字面量键必须存在于 schema（漏键会让默认值悄悄消失）。"""

    CFG_CALL = re.compile(r'(?:self\._cfg|cfg_get\([^,)]+,)\s*\(\s*"([^"]+)"|(?:self\._cfg|cfg_get\([^,)]+,)\s*"([^"]+)"')

    def test_all_literal_keys_declared(self):
        keys = set(schema_keys())
        offenders = []
        for path in PRODUCTION_FILES:
            if path.name == "plugin_config.py":
                continue
            src = path.read_text(encoding="utf-8")
            for m in re.finditer(r'self\._cfg\("([^"]+)"\)', src):
                if m.group(1) not in keys:
                    offenders.append(f"{path.relative_to(ROOT)} self._cfg(\"{m.group(1)}\")")
            for m in re.finditer(r'cfg_get\([^,)]+,\s*"([^"]+)"', src):
                if m.group(1) not in keys:
                    offenders.append(f"{path.relative_to(ROOT)} cfg_get(..., \"{m.group(1)}\")")
        assert offenders == [], "这些配置键不在 _conf_schema.json 里：\n" + "\n".join(offenders)
