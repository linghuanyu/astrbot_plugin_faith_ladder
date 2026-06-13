"""
Scheduler service for daily push and automatic backups.
"""

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable, Awaitable

from astrbot.api import logger


class SchedulerService:
    """Manages scheduled tasks: daily leaderboard push and auto backups."""

    def __init__(
        self,
        data_dir: Path,
        get_leaderboard_text: Callable[[str, int], Awaitable[str]],
        get_config: Callable[[], dict],
        send_to_group: Callable[[str, str], Awaitable[None]],
        get_active_groups: Callable[[], Awaitable[list[str]]],
    ):
        self.data_dir = data_dir
        self.backup_dir = data_dir / "backups"
        self._get_leaderboard_text = get_leaderboard_text
        self._get_config = get_config
        self._send_to_group = send_to_group
        self._get_active_groups = get_active_groups
        self._daily_push_task: Optional[asyncio.Task] = None
        self._backup_task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self):
        """Start the scheduler tasks."""
        self._running = True
        self._daily_push_task = asyncio.create_task(self._daily_push_loop())
        self._backup_task = asyncio.create_task(self._backup_loop())
        logger.info("SchedulerService: tasks started")

    async def stop(self):
        """Stop all scheduler tasks gracefully."""
        self._running = False
        if self._daily_push_task:
            self._daily_push_task.cancel()
            try:
                await self._daily_push_task
            except asyncio.CancelledError:
                pass
        if self._backup_task:
            self._backup_task.cancel()
            try:
                await self._backup_task
            except asyncio.CancelledError:
                pass
        logger.info("SchedulerService: tasks stopped")

    async def _daily_push_loop(self):
        """Loop that checks every minute if it's time for daily push."""
        while self._running:
            try:
                config = self._get_config()
                if not config.get("daily_push_enabled", True):
                    await asyncio.sleep(60)
                    continue

                push_time = config.get("daily_push_time", "07:00")
                now = datetime.now()
                current_time = now.strftime("%H:%M")

                if current_time == push_time:
                    await self._do_daily_push(config)
                    # Sleep 61 seconds to avoid triggering again same minute
                    await asyncio.sleep(61)
                    continue

                await asyncio.sleep(30)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"SchedulerService daily push error: {e}")
                await asyncio.sleep(60)

    async def _backup_loop(self):
        """Loop that runs backup check daily."""
        while self._running:
            try:
                config = self._get_config()
                if config.get("auto_backup_enabled", True):
                    await self._do_backup(config)

                # Run once per day (check every hour)
                await asyncio.sleep(3600)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"SchedulerService backup error: {e}")
                await asyncio.sleep(3600)

    async def _do_daily_push(self, config: dict):
        """Push leaderboard to configured groups. If daily_push_groups is empty, skip push."""
        push_groups = config.get("daily_push_groups", [])

        # If no target groups configured, skip push entirely
        if not push_groups:
            logger.info("Daily push skipped: daily_push_groups is empty")
            return

        limit = config.get("ladder_display_limit", 10)

        for group_id in push_groups:
            try:
                text = await self._get_leaderboard_text(group_id, limit)
                header = "=== 每日排行榜推送 ===\n"
                await self._send_to_group(group_id, header + text)
            except Exception as e:
                logger.error(f"Failed to push to group {group_id}: {e}")

    async def _do_backup(self, config: dict):
        """Create backup and clean up old ones."""
        from astrbot_plugin_faith_ladder.db_manager import DatabaseManager

        retention_days = config.get("backup_retention_days", 7)
        db_path = self.data_dir / "ladder.db"

        if not db_path.exists():
            return

        # Create backup
        backup_dir = self.backup_dir
        backup_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / f"ladder_backup_{timestamp}.db"

        import shutil
        shutil.copy2(db_path, backup_path)
        logger.info(f"Backup created: {backup_path}")

        # Clean old backups
        cutoff = datetime.now().timestamp() - (retention_days * 86400)
        for f in backup_dir.glob("ladder_backup_*.db"):
            if f.stat().st_mtime < cutoff:
                f.unlink()
                logger.info(f"Old backup removed: {f}")

    def should_trigger_now(self, push_time: str) -> bool:
        """Check if current time matches push_time. Useful for testing."""
        now = datetime.now()
        return now.strftime("%H:%M") == push_time
