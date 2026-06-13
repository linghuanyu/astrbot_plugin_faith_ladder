"""
Permission service for whitelist management.
Checks both config-defined whitelist and database whitelist.
"""

from typing import Optional
from astrbot_plugin_faith_ladder.db_manager import DatabaseManager


class PermissionService:
    """Manages whitelist-based permissions for score entry.

    Permission sources (checked in order):
    1. config.admin_ids - global admin list (always has permission)
    2. config.whitelist - WebUI-defined whitelist entries
    3. DB whitelist - runtime whitelist managed via commands
    """

    def __init__(self, db_manager: DatabaseManager, config: Optional[dict] = None):
        self.db = db_manager
        self._config = config or {}

    def set_config(self, config: dict):
        """Update the config reference (called on config reload)."""
        self._config = config

    def is_admin(self, user_id: str) -> bool:
        """Check if user is in the global admin list (from config)."""
        admin_ids = self._config.get("admin_ids", [])
        return str(user_id) in [str(aid) for aid in admin_ids]

    def is_in_config_whitelist(self, group_id: str, user_id: str) -> bool:
        """Check if user/group is in the config-defined whitelist."""
        whitelist = self._config.get("whitelist", [])
        for entry in whitelist:
            if not isinstance(entry, dict):
                continue
            entry_type = str(entry.get("type", ""))
            entry_id = str(entry.get("id", ""))
            if entry_type == "user" and entry_id == str(user_id):
                return True
            if entry_type == "group" and entry_id == str(group_id):
                return True
        return False

    async def check_score_permission(self, group_id: str, user_id: str) -> bool:
        """
        Check if a user has permission to enter scores.
        Checks: config admin_ids → config whitelist → DB whitelist.
        """
        # Check config admin list
        if self.is_admin(user_id):
            return True

        # Check config whitelist
        if self.is_in_config_whitelist(group_id, user_id):
            return True

        # Check DB whitelist
        return await self.db.is_whitelisted(group_id, user_id)

    async def add_to_whitelist(
        self, group_id: str, entry_type: str,
        entry_id: str, added_by: str
    ) -> tuple[bool, str]:
        """
        Add an entry to the DB whitelist (runtime, via command).
        Returns (success, message).
        """
        if entry_type not in ("user", "group"):
            return False, f"无效的类型: {entry_type}。可选: user, group"

        if not entry_id.strip():
            return False, "ID 不能为空。"

        added = await self.db.add_to_whitelist(group_id, entry_type, entry_id, added_by)
        if added:
            return True, f"已添加 [{entry_type}] {entry_id} 到白名单。"
        else:
            return False, f"[{entry_type}] {entry_id} 已在白名单中。"

    async def remove_from_whitelist(
        self, group_id: str, entry_type: str, entry_id: str
    ) -> tuple[bool, str]:
        """
        Remove an entry from the DB whitelist.
        Returns (success, message).
        """
        if entry_type not in ("user", "group"):
            return False, f"无效的类型: {entry_type}。可选: user, group"

        removed = await self.db.remove_from_whitelist(group_id, entry_type, entry_id)
        if removed:
            return True, f"已从白名单移除 [{entry_type}] {entry_id}。"
        else:
            return False, f"未找到 [{entry_type}] {entry_id}。"

    async def get_whitelist_text(self, group_id: str) -> str:
        """Get formatted whitelist text combining config and DB sources."""
        from astrbot_plugin_faith_ladder.message_formatter import format_whitelist_combined
        db_entries = await self.db.get_whitelist(group_id)
        config_entries = self._get_config_whitelist_entries()
        return format_whitelist_combined(config_entries, db_entries)

    def _get_config_whitelist_entries(self) -> list[dict]:
        """Get whitelist entries from config."""
        whitelist = self._config.get("whitelist", [])
        result = []
        for entry in whitelist:
            if isinstance(entry, dict):
                result.append({
                    "entry_type": str(entry.get("type", "user")),
                    "entry_id": str(entry.get("id", "")),
                    "note": str(entry.get("note", "")),
                    "source": "config",
                })
        return result

    async def sync_config_whitelist_to_db(self, group_id: str = "__global__"):
        """
        Sync config whitelist entries to DB on plugin init.
        Ensures config-defined whitelist entries are also stored in DB for consistency.
        """
        whitelist = self._config.get("whitelist", [])
        added_count = 0
        for entry in whitelist:
            if not isinstance(entry, dict):
                continue
            entry_type = str(entry.get("type", ""))
            entry_id = str(entry.get("id", ""))
            if entry_type and entry_id:
                # Use a special group for config-defined entries
                result = await self.db.add_to_whitelist(
                    group_id, entry_type, entry_id, "config_sync"
                )
                if result:
                    added_count += 1
        return added_count
