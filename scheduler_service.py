"""
Scheduler service for automatic backups and cleanup tasks.
"""

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable, Awaitable, Any

from astrbot.api import logger


class SchedulerService:
    """Manages scheduled tasks: auto backups and gift cleanup."""

    def __init__(
        self,
        data_dir: Path,
        get_config: Callable[[], dict],
        purge_score_history: Optional[Callable[[int], Awaitable[int]]] = None,
        purge_expired_statuses: Optional[Callable[[], Awaitable[int]]] = None,
        cleanup_expired_gifts: Optional[Callable[..., Awaitable[int]]] = None,
        notify_gift_timeout: Optional[Callable[[str, str], Awaitable[None]]] = None,
    ):
        self.data_dir = data_dir
        self.backup_dir = data_dir / "backups"
        self._purge_score_history = purge_score_history
        self._purge_expired_statuses = purge_expired_statuses
        self._cleanup_expired_gifts = cleanup_expired_gifts
        self._notify_gift_timeout = notify_gift_timeout
        self._get_config = get_config
        self._backup_task: Optional[asyncio.Task] = None
        self._gift_cleanup_task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self):
        """Start the scheduler tasks."""
        self._running = True
        self._backup_task = asyncio.create_task(self._backup_loop())
        self._gift_cleanup_task = asyncio.create_task(self._gift_cleanup_loop())
        logger.info("SchedulerService: tasks started")

    async def stop(self):
        """Stop all scheduler tasks gracefully."""
        self._running = False
        for task in (self._backup_task, self._gift_cleanup_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        logger.info("SchedulerService: tasks stopped")

    async def _backup_loop(self):
        """Loop that runs backup check and score history purge daily."""
        while self._running:
            try:
                config = self._get_config()
                if config.get("auto_backup_enabled", True):
                    await self._do_backup(config)

                # Purge old score history
                if self._purge_score_history:
                    retention_days = config.get("score_history_retention_days", 90)
                    try:
                        deleted = await self._purge_score_history(retention_days)
                        if deleted > 0:
                            logger.info(f"Purged {deleted} old score history entries (>{retention_days} days)")
                    except Exception as e:
                        logger.error(f"Score history purge error: {e}")

                # Purge expired statuses
                if self._purge_expired_statuses:
                    try:
                        deleted = await self._purge_expired_statuses()
                        if deleted > 0:
                            logger.info(f"Purged {deleted} expired player statuses")
                    except Exception as e:
                        logger.error(f"Status purge error: {e}")

                # Run once per day (check every hour)
                await asyncio.sleep(3600)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"SchedulerService backup error: {e}")
                await asyncio.sleep(3600)

    async def _gift_cleanup_loop(self):
        """Loop that cleans up expired pending gifts every 60 seconds."""
        while self._running:
            try:
                if self._cleanup_expired_gifts:
                    try:
                        refunded = await self._cleanup_expired_gifts(
                            notify=self._notify_gift_timeout
                        )
                        if refunded > 0:
                            logger.info(f"Cleaned up {refunded} expired pending gifts")
                    except Exception as e:
                        logger.error(f"Gift cleanup error: {e}")

                await asyncio.sleep(60)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"SchedulerService gift cleanup error: {e}")
                await asyncio.sleep(60)

    async def _do_backup(self, config: dict):
        """Create backup and clean up old ones using non-blocking I/O."""
        retention_days = config.get("backup_retention_days", 7)
        db_path = self.data_dir / "ladder.db"

        if not db_path.exists():
            return

        # Create backup (non-blocking)
        backup_dir = self.backup_dir
        backup_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / f"ladder_backup_{timestamp}.db"

        import shutil
        await asyncio.to_thread(shutil.copy2, db_path, backup_path)
        logger.info(f"Backup created: {backup_path}")

        # Clean old backups (non-blocking)
        cutoff = datetime.now().timestamp() - (retention_days * 86400)

        def _remove_old():
            for f in backup_dir.glob("ladder_backup_*.db"):
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    logger.info(f"Old backup removed: {f}")

        await asyncio.to_thread(_remove_old)

    def should_trigger_now(self, push_time: str) -> bool:
        """Check if current time matches push_time. Useful for testing."""
        now = datetime.now()
        return now.strftime("%H:%M") == push_time
