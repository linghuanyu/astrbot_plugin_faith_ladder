"""
Permission service for whitelist management.
Checks both config-defined whitelist and database whitelist.
Whitelist is GLOBAL — not scoped to any group.
"""

import time
from typing import Optional, Callable
from astrbot_plugin_faith_ladder.db_manager import DatabaseManager


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
        return str(user_id) in [str(aid) for aid in admin_ids]

    def is_in_config_whitelist(self, user_id: str) -> bool:
        """Check if user is in the config-defined global whitelist."""
        whitelist = self._config.get("whitelist", [])
        for entry in whitelist:
            if not isinstance(entry, dict):
                continue
            entry_type = str(entry.get("type", ""))
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
        # 检查缓存
        now = time.time()
        if user_id in self._permission_cache:
            result, timestamp = self._permission_cache[user_id]
            if now - timestamp < self.CACHE_TTL:
                return result

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
        self, user_id: str, added_by: str
    ) -> tuple[bool, str]:
        """添加用户到诸神列表。返回 (success, message)。"""
        if not user_id.strip():
            return False, "ID 不能为空。"

        added = await self.db.add_to_whitelist("user", user_id, added_by)
        if added:
            return True, f"已添加 {user_id} 到诸神列表。"
        else:
            return False, f"{user_id} 已是诸神。"

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
        """Get formatted whitelist text combining config and DB sources."""
        from astrbot_plugin_faith_ladder.message_formatter import format_whitelist_combined
        db_entries = await self.db.get_whitelist()
        config_entries = self._get_config_whitelist_entries()
        return format_whitelist_combined(config_entries, db_entries)

    def _get_config_whitelist_entries(self) -> list[dict]:
        """Get whitelist entries from config. 仅返回 user 类型（group 类型已废弃）。"""
        whitelist = self._config.get("whitelist", [])
        result = []
        for entry in whitelist:
            if isinstance(entry, dict):
                entry_type = str(entry.get("type", "user"))
                # 仅返回 user 类型，group 类型已废弃不再支持
                if entry_type != "user":
                    continue
                result.append({
                    "entry_type": entry_type,
                    "entry_id": str(entry.get("id", "")),
                    "note": str(entry.get("note", "")),
                    "source": "config",
                })
        return result
