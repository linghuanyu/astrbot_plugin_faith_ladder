"""
配置读取的唯一入口：默认值与类型以 `_conf_schema.json` 为唯一事实来源。

为什么集中：此前 60 处 `self.config.get(key, default)` 各自带默认值，已经出现
"帮助文案显示 600、实际生效 5"这类不一致（query_cooldown_seconds）。默认值只应写
一份，否则每次改默认值都得靠人搜遍全部调用点。

顺带把"WebUI 写了 null / 存成字符串"这类脏值统一挡在读取层：按 schema 声明的类型
转换，坏值回落默认值，调用方不必各自写 int()/str() 兜底。

纯标准库实现（不 import astrbot），便于在无框架环境下测试。
"""

from __future__ import annotations

import copy
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).resolve().parent / "_conf_schema.json"

_MISSING = object()

# key -> (default, type_name)
_SCHEMA: Optional[Dict[str, Tuple[Any, str]]] = None


def _load_schema() -> Dict[str, Tuple[Any, str]]:
    """读取并缓存 schema 表。文件缺失/损坏时返回空表（读取退化为"用调用方默认值"）。"""
    global _SCHEMA
    if _SCHEMA is not None:
        return _SCHEMA

    table: Dict[str, Tuple[Any, str]] = {}
    try:
        raw = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"[Config] 读取 _conf_schema.json 失败，默认值回落调用方: {e}")
        _SCHEMA = table
        return _SCHEMA

    for key, spec in (raw or {}).items():
        if not isinstance(spec, dict):
            continue
        table[str(key)] = (spec.get("default"), str(spec.get("type", "string")))
    _SCHEMA = table
    return _SCHEMA


def reload_schema() -> None:
    """丢弃缓存，下次读取重新解析 schema（测试用）。"""
    global _SCHEMA
    _SCHEMA = None


def schema_keys() -> List[str]:
    return list(_load_schema().keys())


def schema_has(key: str) -> bool:
    return key in _load_schema()


def schema_default(key: str) -> Any:
    entry = _load_schema().get(key)
    return copy.deepcopy(entry[0]) if entry else None


def _cast_bool(value: Any, default: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("1", "true", "yes", "on", "是", "开"):
            return True
        if v in ("0", "false", "no", "off", "否", "关", ""):
            return False
    return default


def _cast(value: Any, type_name: str, default: Any) -> Any:
    """按 schema 声明的类型转换；转不动就回落默认值。"""
    try:
        if type_name == "int":
            # 兼容 "5" / "5.0" / 5.0 三种写法（WebUI 有时会存成字符串）
            return int(float(value)) if isinstance(value, str) else int(value)
        if type_name == "float":
            return float(value)
        if type_name == "bool":
            return _cast_bool(value, default)
        if type_name == "list":
            if isinstance(value, (list, tuple)):
                return list(value)
            if isinstance(value, str):
                # 只按半角逗号与换行切分：中文逗号可能是内容本身（如祷词文案）
                text = value.strip()
                return [p.strip() for p in re.split(r"[,\n]", text) if p.strip()] if text else []
            return default
        if type_name in ("object", "dict"):
            return value if isinstance(value, dict) else default
        if type_name == "string":
            return value if isinstance(value, str) else str(value)
    except (TypeError, ValueError):
        return default
    return value


def cfg_get(config: Optional[dict], key: str, default: Any = None) -> Any:
    """读取配置项。

    - schema 里有该键：以 schema 默认值兜底，并按 schema 声明的类型转换
    - schema 里没有（例如动态拼出的 `prayer_trigger_messages_*`）：原样返回配置值，
      没有则用调用方给的 default
    """
    entry = _load_schema().get(key)
    if entry is None:
        if isinstance(config, dict) and key in config:
            return config[key]
        return default

    schema_default, type_name = entry
    raw = config.get(key, _MISSING) if isinstance(config, dict) else _MISSING
    if raw is _MISSING or raw is None:
        return copy.deepcopy(schema_default)
    return _cast(raw, type_name, copy.deepcopy(schema_default))
