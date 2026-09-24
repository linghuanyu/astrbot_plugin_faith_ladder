"""
Tests for the permission service (global whitelist).
"""

import pytest
from astrbot_plugin_faith_ladder.permission_service import PermissionService


@pytest.mark.asyncio
class TestPermissionService:
    """Tests for PermissionService with global whitelist."""

    async def test_check_permission_not_whitelisted(self, db_manager):
        """Test permission check for non-whitelisted user."""
        service = PermissionService(db_manager)
        result = await service.check_score_permission("u1")
        assert result is False

    async def test_check_permission_user_whitelisted(self, db_manager):
        """Test permission check for whitelisted user."""
        await db_manager.add_to_whitelist("user", "u1", "admin")
        service = PermissionService(db_manager)
        result = await service.check_score_permission("u1")
        assert result is True

    async def test_add_whitelist_user(self, db_manager):
        """Test adding user to global whitelist."""
        service = PermissionService(db_manager)
        success, msg = await service.add_to_whitelist("u123", "admin")
        assert success is True
        assert "u123" in msg
        assert "诸神" in msg

    async def test_add_whitelist_empty_id(self, db_manager):
        """Test adding with empty ID."""
        service = PermissionService(db_manager)
        success, msg = await service.add_to_whitelist("  ", "admin")
        assert success is False
        assert "不能为空" in msg

    async def test_add_whitelist_duplicate(self, db_manager):
        """Test adding duplicate entry."""
        service = PermissionService(db_manager)
        await service.add_to_whitelist("u123", "admin")
        success, msg = await service.add_to_whitelist("u123", "admin")
        assert success is False
        assert "已是诸神" in msg

    async def test_duplicate_add_with_faith_updates_faith(self, db_manager):
        """带信仰重复 add 视为改信仰：否则用户以为改了、实际信仰还在旧的。"""
        service = PermissionService(db_manager)
        await service.add_to_whitelist("u123", "admin", faith="沉默")
        success, msg = await service.add_to_whitelist("u123", "admin", faith="湮灭")

        assert success is True
        assert "湮灭" in msg
        assert await service.get_god_faith("u123") == "湮灭"

    async def test_remove_whitelist_success(self, db_manager):
        """Test removing from whitelist."""
        await db_manager.add_to_whitelist("user", "u123", "admin")
        service = PermissionService(db_manager)
        success, msg = await service.remove_from_whitelist("u123")
        assert success is True
        assert "移除" in msg

    async def test_remove_whitelist_not_found(self, db_manager):
        """Test removing non-existent entry."""
        service = PermissionService(db_manager)
        success, msg = await service.remove_from_whitelist("u999")
        assert success is False
        assert "未找到" in msg

    async def test_get_whitelist_text_empty(self, db_manager):
        """Test getting whitelist text when empty."""
        service = PermissionService(db_manager)
        text = await service.get_whitelist_text()
        assert "诸神列表为空" in text

    async def test_get_whitelist_text_with_entries(self, db_manager):
        """Test getting whitelist text with entries."""
        await db_manager.add_to_whitelist("user", "u1", "admin")
        await db_manager.add_to_whitelist("user", "u2", "admin")
        service = PermissionService(db_manager)
        text = await service.get_whitelist_text()
        assert "u1" in text
        assert "u2" in text
        assert "共 2 位" in text


@pytest.mark.asyncio
class TestAdminAndDbWhitelist:
    """config.admin_ids 是唯一管理权限来源；诸神名单只存在 DB（配置层已废弃）。"""

    async def test_config_admin_has_permission(self, db_manager):
        """Test that config admin_ids always have permission."""
        config = {"admin_ids": ["admin_001", "admin_002"]}
        service = PermissionService(db_manager, config)
        assert await service.check_score_permission("admin_001") is True
        assert await service.check_score_permission("admin_002") is True
        assert await service.check_score_permission("random_user") is False

    async def test_is_admin_method(self, db_manager):
        """Test is_admin method."""
        config = {"admin_ids": ["admin_001"]}
        service = PermissionService(db_manager, config)
        assert service.is_admin("admin_001") is True
        assert service.is_admin("admin_002") is False
        assert service.is_admin("random") is False

    async def test_deprecated_config_whitelist_is_ignored(self, db_manager):
        """已废弃的 WebUI whitelist 配置：既不授权，也不出现在诸神列表里。"""
        config = {
            "whitelist": [
                {"type": "user", "id": "u123", "note": "积分管理员"},
            ]
        }
        service = PermissionService(db_manager, config)
        assert await service.check_score_permission("u123") is False
        text = await service.get_whitelist_text()
        assert "u123" not in text
        assert "诸神列表为空" in text

    async def test_only_db_whitelist_grants(self, db_manager):
        """授权来源只有 admin_ids 与 DB 白名单；配置层条目不再参与。"""
        config = {"whitelist": [{"type": "user", "id": "config_user", "note": ""}]}
        await db_manager.add_to_whitelist("user", "db_user", "admin")
        service = PermissionService(db_manager, config)

        assert await service.check_score_permission("config_user") is False
        assert await service.check_score_permission("db_user") is True
        assert await service.check_score_permission("random") is False

    async def test_empty_config(self, db_manager):
        """Test with empty config."""
        service = PermissionService(db_manager, {})
        assert service.is_admin("anyone") is False

    async def test_none_config(self, db_manager):
        """Test with None config (backward compatibility)."""
        service = PermissionService(db_manager)
        assert service.is_admin("anyone") is False

    async def test_set_config_updates(self, db_manager):
        """Test that set_config updates the config reference."""
        service = PermissionService(db_manager, {"admin_ids": ["old_admin"]})
        assert service.is_admin("old_admin") is True
        assert service.is_admin("new_admin") is False

        service.set_config({"admin_ids": ["new_admin"]})
        assert service.is_admin("old_admin") is False
        assert service.is_admin("new_admin") is True

    async def test_whitelist_text_lists_db_entries_only(self, db_manager):
        """诸神列表只列 DB 条目；配置层已废弃的条目不再出现。"""
        config = {"whitelist": [{"type": "user", "id": "config_u1", "note": "旧配置"}]}
        await db_manager.add_to_whitelist("user", "db_u1", "admin")
        service = PermissionService(db_manager, config)
        text = await service.get_whitelist_text()
        assert "db_u1" in text
        assert "config_u1" not in text
        assert "共 1 位" in text
