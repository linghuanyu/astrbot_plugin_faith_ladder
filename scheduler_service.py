"""
Scheduler service for daily push and automatic backups.
"""

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable, Awaitable, Any

from astrbot.api import logger


class SchedulerService:
    """Manages scheduled tasks: daily leaderboard push and auto backups."""

    def __init__(
        self,
        data_dir: Path,
        get_leaderboard_text: Callable[[str, int], Awaitable[str]],
        get_config: Callable[[], dict],
        send_to_group: Callable[[str, Any], Awaitable[None]],
        get_active_groups: Callable[[], Awaitable[list[str]]],
        get_pilgrimage_text: Optional[Callable[[str, int], Awaitable[str]]] = None,
        get_leaderboard_players: Optional[Callable[[str, int], Awaitable[list]]] = None,
        get_pilgrimage_players: Optional[Callable[[str, int], Awaitable[list]]] = None,
        image_renderer: Optional[Any] = None,
        get_output_mode: Optional[Callable[[str], Awaitable[str]]] = None,
        purge_score_history: Optional[Callable[[int], Awaitable[int]]] = None,
    ):
        self.data_dir = data_dir
        self.backup_dir = data_dir / "backups"
        self._get_leaderboard_text = get_leaderboard_text
        self._get_pilgrimage_text = get_pilgrimage_text
        self._get_leaderboard_players = get_leaderboard_players
        self._get_pilgrimage_players = get_pilgrimage_players
        self._image_renderer = image_renderer
        self._get_output_mode = get_output_mode
        self._purge_score_history = purge_score_history
        self._get_config = get_config
        self._send_to_group = send_to_group
        self._get_active_groups = get_active_groups
        self._daily_push_task: Optional[asyncio.Task] = None
        self._backup_task: Optional[asyncio.Task] = None
        self._running = False
        self._last_push_date: Optional[datetime] = None  # Track last push date to prevent double-fire

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
        """Loop that checks every 30 seconds if it's time for daily push.
        Uses date tracking to prevent double-fire or missed pushes."""
        while self._running:
            try:
                config = self._get_config()
                if not config.get("daily_push_enabled", True):
                    await asyncio.sleep(60)
                    continue

                push_time_str = config.get("daily_push_time", "07:00")
                now = datetime.now()

                # Parse scheduled time for today
                try:
                    scheduled_time = datetime.strptime(push_time_str, "%H:%M").replace(
                        year=now.year, month=now.month, day=now.day
                    )
                except ValueError:
                    logger.error(f"Invalid daily_push_time format: {push_time_str}")
                    await asyncio.sleep(3600)
                    continue

                # Push if: current time >= scheduled time AND we haven't pushed today
                if now >= scheduled_time and self._last_push_date != now.date():
                    self._last_push_date = now.date()
                    await self._do_daily_push(config)

                await asyncio.sleep(30)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"SchedulerService daily push error: {e}")
                await asyncio.sleep(60)

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

                # Run once per day (check every hour)
                await asyncio.sleep(3600)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"SchedulerService backup error: {e}")
                await asyncio.sleep(3600)

    async def _get_effective_output_mode(self, group_id: str, config: dict) -> str:
        """Get effective output mode for a group (DB override or global default)."""
        if self._get_output_mode:
            try:
                mode = await self._get_output_mode(group_id)
                if mode in ("text", "image"):
                    return mode
            except Exception as e:
                logger.error(f"Failed to get output mode for group {group_id}: {e}")
        return config.get("output_mode", "text")

    async def _do_daily_push(self, config: dict):
        """Push both ladder and pilgrimage leaderboards to configured groups."""
        push_groups = config.get("daily_push_groups", [])

        if not push_groups:
            logger.info("Daily push skipped: daily_push_groups is empty")
            return

        limit = config.get("ladder_display_limit", 10)

        for group_id in push_groups:
            try:
                output_mode = await self._get_effective_output_mode(group_id, config)
                header = "=== 每日排行榜推送 ==="

                if output_mode == "image" and self._image_renderer:
                    await self._push_image_mode(group_id, header, limit)
                else:
                    await self._push_text_mode(group_id, header, limit)

            except Exception as e:
                logger.error(f"Failed to push to group {group_id}: {e}")

    async def _push_text_mode(self, group_id: str, header: str, limit: int):
        """Send text-mode leaderboards to a group."""
        # Ladder leaderboard
        ladder_text = await self._get_leaderboard_text(group_id, limit)
        await self._send_to_group(group_id, f"{header}\n\n⚔️ 天梯排行榜")
        await self._send_to_group(group_id, ladder_text)

        # Pilgrimage leaderboard (if available)
        if self._get_pilgrimage_text:
            pilgrimage_text = await self._get_pilgrimage_text(group_id, limit)
            await self._send_to_group(group_id, pilgrimage_text)

    async def _push_image_mode(self, group_id: str, header: str, limit: int):
        """Send image-mode leaderboards to a group, falling back to text on failure."""
        rendered_any = False

        # Ladder leaderboard image
        if self._get_leaderboard_players:
            try:
                players = await self._get_leaderboard_players(group_id, limit)
                if players:
                    image_bytes = await self._image_renderer.render_leaderboard_image(
                        players, limit
                    )
                    if image_bytes:
                        await self._send_to_group(group_id, header)
                        await self._send_to_group(group_id, ("image", image_bytes))
                        rendered_any = True
                    else:
                        # Fallback to text
                        text = await self._get_leaderboard_text(group_id, limit)
                        await self._send_to_group(group_id, f"{header}\n\n{text}\n[图片渲染失败，已降级为文本]")
                        rendered_any = True
                else:
                    await self._send_to_group(group_id, f"{header}\n暂无排名数据。")
                    rendered_any = True
            except Exception as e:
                logger.error(f"Ladder image render failed for group {group_id}: {e}")

        # Pilgrimage leaderboard image
        if self._get_pilgrimage_players and self._get_pilgrimage_text:
            try:
                players = await self._get_pilgrimage_players(group_id, limit)
                if players:
                    image_bytes = await self._image_renderer.render_pilgrimage_image(
                        players, limit
                    )
                    if image_bytes:
                        await self._send_to_group(group_id, ("image", image_bytes))
                    else:
                        text = await self._get_pilgrimage_text(group_id, limit)
                        await self._send_to_group(group_id, f"{text}\n[图片渲染失败，已降级为文本]")
                else:
                    if not rendered_any:
                        await self._send_to_group(group_id, "暂无排名数据。")
            except Exception as e:
                logger.error(f"Pilgrimage image render failed for group {group_id}: {e}")
                # Fallback to text
                if self._get_pilgrimage_text:
                    text = await self._get_pilgrimage_text(group_id, limit)
                    await self._send_to_group(group_id, text)

        if not rendered_any:
            # No image support available, fall back to text
            await self._push_text_mode(group_id, header, limit)

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
