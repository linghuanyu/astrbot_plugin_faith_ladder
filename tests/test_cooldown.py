"""
Tests for the cooldown manager.
"""

import pytest
import time
from astrbot_plugin_faith_ladder.cooldown import CooldownManager


class TestCooldownManager:
    """Tests for the CooldownManager."""

    def test_no_cooldown_initially(self):
        """Test that user has no cooldown initially."""
        cm = CooldownManager()
        assert cm.check_cooldown("u1", 5) is True

    def test_cooldown_after_set(self):
        """Test that cooldown is active after setting."""
        cm = CooldownManager()
        cm.set_cooldown("u1")
        assert cm.check_cooldown("u1", 5) is False

    def test_cooldown_expires(self):
        """Test that cooldown expires after the period."""
        cm = CooldownManager()
        cm.set_cooldown("u1")

        # Manually set the cooldown time to past
        cm._cooldowns["u1"] = time.time() - 10
        assert cm.check_cooldown("u1", 5) is True

    def test_different_users_independent(self):
        """Test that cooldowns are per-user."""
        cm = CooldownManager()
        cm.set_cooldown("u1")
        assert cm.check_cooldown("u1", 5) is False
        assert cm.check_cooldown("u2", 5) is True

    def test_get_remaining(self):
        """Test getting remaining cooldown time."""
        cm = CooldownManager()
        cm.set_cooldown("u1")
        remaining = cm.get_remaining("u1", 5)
        assert 0 < remaining <= 5

    def test_get_remaining_no_cooldown(self):
        """Test remaining is 0 when no cooldown set."""
        cm = CooldownManager()
        assert cm.get_remaining("u1", 5) == 0

    def test_get_remaining_expired(self):
        """Test remaining is 0 when cooldown expired."""
        cm = CooldownManager()
        cm._cooldowns["u1"] = time.time() - 10
        assert cm.get_remaining("u1", 5) == 0

    def test_clear_cooldown(self):
        """Test clearing a user's cooldown."""
        cm = CooldownManager()
        cm.set_cooldown("u1")
        assert cm.check_cooldown("u1", 5) is False

        cm.clear_cooldown("u1")
        assert cm.check_cooldown("u1", 5) is True

    def test_clear_cooldown_nonexistent(self):
        """Test clearing cooldown for user without one doesn't error."""
        cm = CooldownManager()
        cm.clear_cooldown("u1")  # Should not raise

    def test_clear_all(self):
        """Test clearing all cooldowns."""
        cm = CooldownManager()
        cm.set_cooldown("u1")
        cm.set_cooldown("u2")
        cm.set_cooldown("u3")

        cm.clear_all()
        assert cm.check_cooldown("u1", 5) is True
        assert cm.check_cooldown("u2", 5) is True
        assert cm.check_cooldown("u3", 5) is True

    def test_zero_cooldown(self):
        """Test with zero cooldown period."""
        cm = CooldownManager()
        cm.set_cooldown("u1")
        # With 0 second cooldown, should always pass
        assert cm.check_cooldown("u1", 0) is True
