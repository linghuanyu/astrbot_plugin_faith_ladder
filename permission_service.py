"""
Permission service for whitelist management.
Checks both config-defined whitelist and database whitelist.
Whitelist is GLOBAL — not scoped to any group.
"""

import time
from typing import Optional, Callable
from astrbot_plugin_faith_ladder.db_manager import DatabaseManager


def _normalize_entry_type(value) -> str:
    """白名单条目的类型归一化：缺失或空值都视为 user（group 类型已废弃）。

    授权侧与展示侧此前各自判断（一处 `or "user"`、一处 `str(get(...,"user"))`），
    空 type 的条目会"权限生效但在列表里看不到"。
    """
    text = str(value).strip() if value is not None else ""
    return text or "user"


class PermissionService:
    """Manages whitelist-based permissions for score entry.

    Permission sources (checked in order):
    1. config.admin_ids - global admin list (always has permission)
    2. config.whitelist - WebUI-defined whitelist entries (global)
    3. DB whitelist - runtime whitelist managed via commands (global)
    """

    # 权限缓存 TTL（秒）
    CACHE_TTL = 300  # 5 分钟

    def __init__(self, db_manager: DatabaseManager, config: Optional[dict] = None, config_getter: Optional[Callable[[], dict]] = None):
        """config 与 config_getter 二选一：前者为静态快照，后者用于配置热重载且优先级更高。"""
        self.db = db_manager
        self._config_getter = config_getter
        self._config_static = config or {}
        # 权限缓存：{user_id: (result, timestamp)}
        self._permission_cache = {}

    def invalidate_cache(self, user_id: str = None):
        """失效权限缓存。不传 user_id 则清空全部缓存。"""
        if user_id:
            self._permission_cache.pop(user_id, None)
        else:
            self._permission_cache.clear()

    @property
    def _config(self) -> dict:
        """Get current config. Uses getter if available (for hot reload), else static copy."""
        if self._config_getter:
            try:
                return self._config_getter()
            except Exception:
                pass
        return self._config_static

    def set_config(self, config: dict):
        """Update the static config reference (called on config reload)."""
        self._config_static = config

    def set_config_getter(self, getter: Callable[[], dict]):
        """Set a callable that returns the current config (preferred over static dict)."""
        self._config_getter = getter

    def is_admin(self, user_id: str) -> bool:
        """Check if user is in the global admin list (from config)."""
        admin_ids = self._config.get("admin_ids", [])
        # 配成字符串（如 "123,456"）时逐个字符会被当成管理员 ID：
        # 用户 "1" 反而成了管理员，真正的 "123,456" 不是。这里统一按列表处理。
        if isinstance(admin_ids, str):
            admin_ids = [part.strip() for part in admin_ids.split(",")]
        return str(user_id) in [str(aid).strip() for aid in admin_ids]

    def is_in_config_whitelist(self, user_id: str) -> bool:
        """Check if user is in the config-defined global whitelist."""
        whitelist = self._config.get("whitelist", [])
        for entry in whitelist:
            if not isinstance(entry, dict):
                continue
            # 缺省 type 视为 user：与 _get_config_whitelist_entries 的展示逻辑保持一致，
            # 否则 WebUI 里不填 type 的条目会「显示在诸神列表里但不生效」
            entry_type = _normalize_entry_type(entry.get("type"))
            entry_id = str(entry.get("id", ""))
            if entry_type == "user" and entry_id == str(user_id):
                return True
        return False

    async def check_score_permission(self, user_id: str, group_id: str = None) -> bool:
        """
        Check if a user has permission to enter scores.
        Global check: config admin_ids → config whitelist → DB whitelist.
        group_id is accepted but ignored (kept for backward compatibility).
        结果缓存 5 分钟，减少 DB 查询。
        """
        # 检查缓存（过期条目顺手删掉，避免字典无上限增长）
        now = time.time()
        cached = self._permission_cache.get(user_id)
        if cached is not None:
            result, timestamp = cached
            if now - timestamp < self.CACHE_TTL:
                return result
            del self._permission_cache[user_id]

        # 原有逻辑
        if self.is_admin(user_id):
            result = True
        elif self.is_in_config_whitelist(user_id):
            result = True
        else:
            result = await self.db.is_whitelisted(user_id)

        # 写入缓存
        self._permission_cache[user_id] = (result, now)
        return result

    async def add_to_whitelist(
        self, user_id: str, added_by: str, faith: str = None
    ) -> tuple[bool, str]:
        """添加用户到诸神列表。返回 (success, message)。"""
        if not user_id.strip():
            return False, "ID 不能为空。"

        added = await self.db.add_to_whitelist("user", user_id, added_by, faith=faith)
        if added:
            faith_str = f"（信仰：{faith}）" if faith else ""
            return True, f"已添加 {user_id} 到诸神列表{faith_str}。"
        else:
            return False, f"{user_id} 已是诸神之一。"

    async def set_whitelist_faith(
        self, user_id: str, faith: str
    ) -> tuple[bool, str]:
        """为诸神设置信仰。返回 (success, message)。"""
        if not user_id.strip():
            return False, "ID 不能为空。"
        if not faith or not faith.strip():
            return False, "信仰不能为空。"

        updated = await self.db.set_whitelist_faith(user_id, faith)
        if updated:
            return True, f"已设置 {user_id} 的信仰为：{faith}。"
        else:
            return False, f"未找到 {user_id}，请先添加到诸神列表。"

    async def remove_whitelist_faith(
        self, user_id: str
    ) -> tuple[bool, str]:
        """移除诸神的信仰标记。返回 (success, message)。"""
        if not user_id.strip():
            return False, "ID 不能为空。"

        updated = await self.db.set_whitelist_faith(user_id, None)
        if updated:
            return True, f"已移除 {user_id} 的信仰标记。"
        else:
            return False, f"未找到 {user_id}。"

    async def get_god_faith(self, user_id: str) -> Optional[str]:
        """获取诸神对应的信仰名（用于选取信仰主题文案）。

        先查 DB（指令添加的诸神），再回退到 WebUI 配置里的同名条目——
        配置里配了信仰却只显示在列表、执行操作时走通用文案，是之前的不一致来源。
        """
        faith = await self.db.get_whitelist_faith(user_id)
        if faith:
            return faith
        for entry in self._get_config_whitelist_entries():
            if entry["entry_id"] == str(user_id):
                return entry.get("faith") or None
        return None

    async def remove_from_whitelist(
        self, user_id: str
    ) -> tuple[bool, str]:
        """从诸神列表移除用户。返回 (success, message)。"""
        removed = await self.db.remove_from_whitelist("user", user_id)
        if removed:
            return True, f"已从诸神列表移除 {user_id}。"
        else:
            return False, f"未找到 {user_id}。"

    async def get_whitelist_text(self) -> str:
        """Get formatted whitelist text: WebUI 配置项 + 运行时用指令添加的条目。"""
        from astrbot_plugin_faith_ladder.message_formatter import format_whitelist_combined
        config_entries = self._get_config_whitelist_entries()
        db_entries = await self._get_db_whitelist_entries()
        return format_whitelist_combined(config_entries, db_entries)

    async def _get_db_whitelist_entries(self) -> list[dict]:
        """Get runtime whitelist entries from DB. 仅返回 user 类型（group 类型已废弃）。"""
        rows = await self.db.get_whitelist_with_faith()
        result = []
        for row in rows:
            if str(row.get("entry_type", "")) != "user":
                continue
            result.append({
                "entry_type": "user",
                "entry_id": str(row.get("entry_id", "")),
                "faith": row.get("faith") or None,
                "note": "",
                "source": "db",
            })
        return result

    def _get_config_whitelist_entries(self) -> list[dict]:
        """Get whitelist entries from config. 仅返回 user 类型（group 类型已废弃）。"""
        whitelist = self._config.get("whitelist", [])
        result = []
        for entry in whitelist:
            if isinstance(entry, dict):
                # 与 is_in_config_whitelist 用同一套缺省判定：空 type 视为 user，
                # 否则会出现"授权通过但列表里看不到"的不一致
                entry_type = _normalize_entry_type(entry.get("type"))
                # 仅返回 user 类型，group 类型已废弃不再支持
                if entry_type != "user":
                    continue
                # 键存在但值为 None（WebUI 写 null）时 .strip() 会抛 AttributeError，
                # 而这里被 白名单 list 与 get_god_faith 共用，崩了会影响多个指令
                faith = (entry.get("faith") or "").strip() or None
                result.append({
                    "entry_type": entry_type,
                    "entry_id": str(entry.get("id", "")),
                    "faith": faith,
                    "note": str(entry.get("note", "")),
                    "source": "config",
                })
        return result
