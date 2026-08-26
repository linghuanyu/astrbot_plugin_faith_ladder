"""
Tests for gift daily accept limit feature.
"""

import pytest
import pytest_asyncio


@pytest.mark.asyncio
class TestGiftDailyAccepts:
    """Tests for gift daily accept DB operations."""

    async def test_no_accept_initially(self, db_manager):
        """Player has no accept initially."""
        await db_manager.upsert_player("g1", "u1", "Alice")
        assert await db_manager.has_gift_accept_today("g1", "u1") is False
        assert await db_manager.count_gift_accepts_today("g1", "u1") == 0

    async def test_record_accept(self, db_manager):
        """Recording an accept increments the count."""
        await db_manager.upsert_player("g1", "u1", "Alice")
        recorded = await db_manager.record_gift_accept("g1", "u1")
        assert recorded is True
        assert await db_manager.has_gift_accept_today("g1", "u1") is True
        assert await db_manager.count_gift_accepts_today("g1", "u1") == 1

    async def test_multiple_records_same_day(self, db_manager):
        """Multiple records on same day are allowed (no UNIQUE constraint)."""
        await db_manager.upsert_player("g1", "u1", "Alice")
        assert await db_manager.record_gift_accept("g1", "u1") is True
        assert await db_manager.record_gift_accept("g1", "u1") is True
        assert await db_manager.record_gift_accept("g1", "u1") is True
        assert await db_manager.count_gift_accepts_today("g1", "u1") == 3

    async def test_different_receivers_independent(self, db_manager):
        """Different receivers have independent accept tracking."""
        await db_manager.upsert_player("g1", "u1", "Alice")
        await db_manager.upsert_player("g1", "u2", "Bob")
        await db_manager.record_gift_accept("g1", "u1")
        await db_manager.record_gift_accept("g1", "u1")
        assert await db_manager.count_gift_accepts_today("g1", "u1") == 2
        assert await db_manager.count_gift_accepts_today("g1", "u2") == 0

    async def test_different_groups_independent(self, db_manager):
        """Same receiver in different groups has independent accept tracking."""
        await db_manager.upsert_player("g1", "u1", "Alice")
        await db_manager.upsert_player("g2", "u1", "Alice")
        await db_manager.record_gift_accept("g1", "u1")
        await db_manager.record_gift_accept("g1", "u1")
        assert await db_manager.count_gift_accepts_today("g1", "u1") == 2
        assert await db_manager.count_gift_accepts_today("g2", "u1") == 0
