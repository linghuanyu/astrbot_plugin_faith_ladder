"""
Cooldown manager for query anti-spam.
"""

import time
from typing import Dict


class CooldownManager:
    """Manages per-user query cooldowns."""

    def __init__(self):
        self._cooldowns: Dict[str, float] = {}

    def check_cooldown(self, user_id: str, cooldown_seconds: int) -> bool:
        """
        Check if a user is still in cooldown.
        Returns True if the user CAN proceed (not in cooldown).
        Returns False if the user is still in cooldown.
        """
        last_used = self._cooldowns.get(user_id, 0)
        return (time.time() - last_used) >= cooldown_seconds

    def set_cooldown(self, user_id: str):
        """Set the cooldown timer for a user to now."""
        self._cooldowns[user_id] = time.time()

    def get_remaining(self, user_id: str, cooldown_seconds: int) -> float:
        """Get remaining cooldown time in seconds. Returns 0 if not in cooldown."""
        last_used = self._cooldowns.get(user_id, 0)
        remaining = cooldown_seconds - (time.time() - last_used)
        return max(0, remaining)

    def clear_cooldown(self, user_id: str):
        """Clear cooldown for a user."""
        self._cooldowns.pop(user_id, None)

    def clear_all(self):
        """Clear all cooldowns."""
        self._cooldowns.clear()
