"""
Permission service for whitelist management.

白名单只有一个存储：数据库 whitelist 表的 entry_type = 'user' 条目。此前还有
一份 WebUI 配置里的 whitelist 名单，但只有部署者一个人会填、而部署者本身就是
超管，那一层是冗余的，已移除。权限来源因此收敛为两处：

1. config.admin_ids — 超管
2. DB whitelist     — 诸神

白名单是全局的，不按群作用域。
"""

import time
from typing import Optional, Callable
from astrbot_plugin_faith_ladder.db_manager import DatabaseManager
from astrbot_plugin_faith_ladder.plugin_config import cfg_get, config_snapshot


class PermissionService:
    """Manages whitelist-based permissions for score entry.

    Permission sources (checked in order):
    1. config.admin_ids - 超管（唯一的管理权限来源，天然具备诸神权限）
    2. DB whitelist     - 诸神（指令与群成员同步写入，全局生效）
    """

    # 权限缓存 TTL（秒）
    CACHE_TTL = 300  # 5 分钟

    # 参与权限判定的配置键：这几个键一变，缓存立即失效——
    # 否则 WebUI 刚把某人加进 admin_ids，他要等最多 5 分钟才生效
    CONFIG_KEYS = ("admin_ids",)

    # 缓存条数上限。过期清理只发生在"再次查同一个 user"时，从未重复出现的
    # user 永不回收；自动同步群持续进人时这个字典会单调增长，所以兜一个上限。
    MAX_CACHE_SIZE = 1024

    def __init__(self, db_manager: DatabaseManager, config: Optional[dict] = None, config_getter: Optional[Callable[[], dict]] = None):
        """config 与 config_getter 二选一：前者为静态快照，后者用于配置热重载且优先级更高。"""
        self.db = db_manager
        self._config_getter = config_getter
        self._config_static = config or {}
        # 权限缓存：{user_id: (result, timestamp, 配置快照)}
        self._permission_cache = {}
        # 缓存代数：每次失效自增，用于识别"查询途中缓存被失效"
        self._cache_generation = 0

    def invalidate_cache(self, user_id: str = None):
        """失效权限缓存。不传 user_id 则清空全部缓存。

        同时递增代数：check_score_permission 会在 await DB 之前记下代数，
        回写缓存前比对。否则"失效发生在查询中途"时，那个失效前的旧结论会被
        当成新结果写回去，刚被移出白名单的人还能继续通过最长 5 分钟。
        """
        self._cache_generation += 1
        if user_id:
            self._permission_cache.pop(user_id, None)
        else:
            self._permission_cache.clear()

    def _evict_cache_slot(self) -> None:
        """为即将写入的新条目腾空间：先清全部过期项，仍满则丢最旧的一条。"""
        if len(self._permission_cache) < self.MAX_CACHE_SIZE:
            return
        now = time.time()
        for uid in [uid for uid, (_, ts, _) in self._permission_cache.items()
                    if now - ts >= self.CACHE_TTL]:
            del self._permission_cache[uid]
        if len(self._permission_cache) >= self.MAX_CACHE_SIZE:
            oldest = min(self._permission_cache, key=lambda uid: self._permission_cache[uid][1])
            del self._permission_cache[oldest]

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
        admin_ids = cfg_get(self._config, "admin_ids")
        # 配成字符串（如 "123,456"）时逐个字符会被当成管理员 ID：
        # 用户 "1" 反而成了管理员，真正的 "123,456" 不是。这里统一按列表处理。
        if isinstance(admin_ids, str):
            admin_ids = [part.strip() for part in admin_ids.split(",")]
        return str(user_id) in [str(aid).strip() for aid in admin_ids]

    async def check_score_permission(self, user_id: str, group_id: str = None) -> bool:
        """
        Check if a user has permission to enter scores.
        Global check: config admin_ids → DB whitelist.
        group_id is accepted but ignored (kept for backward compatibility).
        结果缓存 5 分钟，减少 DB 查询。
        """
        # 检查缓存（过期条目顺手删掉，避免字典无上限增长）
        now = time.time()
        snapshot = config_snapshot(self._config, self.CONFIG_KEYS)
        cached = self._permission_cache.get(user_id)
        if cached is not None:
            result, timestamp, cached_snapshot = cached
            if now - timestamp < self.CACHE_TTL and cached_snapshot == snapshot:
                return result
            del self._permission_cache[user_id]

        # 记下当前代数：DB 查询期间若缓存被失效（白名单增删），结论可能已过期
        generation = self._cache_generation

        # 原有逻辑
        if self.is_admin(user_id):
            result = True
        else:
            result = await self.db.is_whitelisted(user_id)

        # 写入缓存（带配置快照：配置变了下次就读不到这条）。
        # 代数变了说明查询途中发生过失效，这个结果不能再进缓存。
        if generation == self._cache_generation:
            self._evict_cache_slot()
            self._permission_cache[user_id] = (result, now, snapshot)
        return result

    async def add_to_whitelist(
        self, user_id: str, added_by: str, faith: str = None
    ) -> tuple[bool, str]:
        """添加用户到诸神列表。返回 (success, message)。

        已在名单里且带了信仰时按「改信仰」处理：否则 `白名单 add <id> <新信仰>`
        会回一句"已是诸神之一"，用户以为信仰改了、实际没改。
        """
        if not user_id.strip():
            return False, "ID 不能为空。"

        added = await self.db.add_to_whitelist("user", user_id, added_by, faith=faith)
        if added:
            faith_str = f"（信仰：{faith}）" if faith else ""
            return True, f"已将 {user_id} 列入诸神列表{faith_str}。"
        if faith and await self.db.set_whitelist_faith(user_id, faith):
            return True, f"{user_id} 已是诸神，信仰已更新为：{faith}。"
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
        """获取诸神对应的信仰名（用于选取信仰主题文案）。信仰只存在 DB 里。"""
        return await self.db.get_whitelist_faith(user_id)

    async def remove_from_whitelist(
        self, user_id: str
    ) -> tuple[bool, str]:
        """从诸神列表移除用户。返回 (success, message)。

        `remove` 的语义是「把这个 id 从我名单里拿掉」，所以待审行一并清掉——
        否则同一 id 若同时有 user 与 pending 两行（手动 add 后对方又入群），
        移除之后他还会出现在待审列表里。
        """
        removed = await self.db.remove_whitelist_entry_everywhere(user_id)
        if removed:
            return True, f"已从诸神列表移除 {user_id}。"
        else:
            return False, f"未找到 {user_id}。"

    async def get_whitelist_text(self) -> str:
        """Get formatted whitelist text（数据全部来自 DB）。"""
        from astrbot_plugin_faith_ladder.message_formatter import format_whitelist
        return format_whitelist(await self._get_db_whitelist_entries())

    # --- 待审名单：入群先进待审，超管确认后才授权 ---

    async def list_pending_text(self) -> str:
        """待审名单的展示文本。"""
        from astrbot_plugin_faith_ladder.message_formatter import format_pending_whitelist
        return format_pending_whitelist(await self.db.get_pending_whitelist())

    async def approve_pending(self, user_id: str) -> tuple[bool, str]:
        """把一条待审条目转为诸神。返回 (success, message)。"""
        if not user_id.strip():
            return False, "ID 不能为空。"
        if await self.db.approve_pending(user_id):
            return True, f"已通过 {user_id} 的入群申请，列入诸神列表。"
        return False, f"{user_id} 不在待审名单中。"

    # 待审一次通过多少人以上必须先看名单再确认。同步白名单会把「群成员里还不是
    # 诸神的」全写进待审，所以一句「全部通过」可能等于全群封神，挡一下。
    BULK_APPROVE_CONFIRM_THRESHOLD = 5

    # 回复里回显多少个 id（再多也只报总数）
    APPROVE_ECHO_LIMIT = 20

    def _echo_ids(self, entry_ids: list) -> str:
        """把待通过/已通过的 id 回显成一行，过长则截断。"""
        shown = "、".join(entry_ids[:self.APPROVE_ECHO_LIMIT])
        if len(entry_ids) > self.APPROVE_ECHO_LIMIT:
            return f"{shown}… 等 {len(entry_ids)} 人"
        return shown

    async def approve_all_pending(self, confirm: bool = False) -> tuple[int, str]:
        """通过全部待审条目。返回 (通过人数, message)。

        待审人数超过阈值且未带确认时**不落库**，只回一条名单预览与确认方式——
        回显本身不构成拦截，所以真正的设防在"这一句不执行"。
        """
        pending = [r["entry_id"] for r in await self.db.get_pending_whitelist()]
        if not pending:
            return 0, "没有待审的入群申请。"

        if len(pending) > self.BULK_APPROVE_CONFIRM_THRESHOLD and not confirm:
            return 0, (
                f"本次将一次授权 {len(pending)} 人：{self._echo_ids(pending)}\n"
                f"确认请发送：白名单 全部通过 确认"
            )

        count = await self.db.approve_all_pending()
        return count, f"已通过 {count} 人的入群申请：{self._echo_ids(pending)}"

    async def reject_pending(self, user_id: str) -> tuple[bool, str]:
        """丢弃一条待审条目。返回 (success, message)。"""
        if not user_id.strip():
            return False, "ID 不能为空。"
        if await self.db.reject_pending(user_id):
            return True, f"已拒绝 {user_id} 的入群申请。"
        return False, f"{user_id} 不在待审名单中。"

    async def _get_db_whitelist_entries(self) -> list[dict]:
        """Get whitelist entries from DB.

        `get_whitelist_with_faith` 已在 SQL 层过滤 entry_type = 'user'，
        非授权条目（如待审）不会走到这里。
        """
        rows = await self.db.get_whitelist_with_faith()
        return [
            {
                "entry_type": "user",
                "entry_id": str(row.get("entry_id", "")),
                "faith": row.get("faith") or None,
                "source": "db",
            }
            for row in rows
        ]
