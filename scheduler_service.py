"""
Scheduler service for automatic backups and cleanup tasks.
"""

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable, Awaitable, Any

from astrbot.api import logger

from astrbot_plugin_faith_ladder.plugin_config import cfg_get


class SchedulerService:
    """Manages scheduled tasks: auto backups and gift cleanup."""

    def __init__(
        self,
        data_dir: Path,
        get_config: Callable[[], dict],
        purge_score_history: Optional[Callable[[int], Awaitable[int]]] = None,
        purge_daily_tables: Optional[Callable[[int], Awaitable[int]]] = None,
        purge_expired_statuses: Optional[Callable[[], Awaitable[int]]] = None,
        cleanup_expired_gifts: Optional[Callable[..., Awaitable[int]]] = None,
        notify_gift_timeout: Optional[Callable[[str, str], Awaitable[None]]] = None,
        on_gift_refunded: Optional[Callable[[str, str], None]] = None,
        backup_db: Optional[Callable[[Path], Awaitable[None]]] = None,
    ):
        """注入各类回调与配置读取器；真正的定时任务在 start() 中创建。

        backup_db(dest) 负责产出数据库备份，由 DB 层实现（独立连接的在线备份），
        调度器只负责取名与清理过期文件。
        """
        self.data_dir = data_dir
        self.backup_dir = data_dir / "backups"
        self._purge_score_history = purge_score_history
        self._purge_daily_tables = purge_daily_tables
        self._purge_expired_statuses = purge_expired_statuses
        self._cleanup_expired_gifts = cleanup_expired_gifts
        self._notify_gift_timeout = notify_gift_timeout
        self._on_gift_refunded = on_gift_refunded
        self._backup_db = backup_db
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
        """每天执行一次：备份 + 清理积分历史 + 清理过期状态。

        循环每 10 分钟醒来一次，但只有「北京日期」变化时才真正执行，
        因此启动后会立即做一次、之后每天一次。
        此前是无条件每小时执行一次，等于每天产生 24 份备份并把清理逻辑跑 24 遍。
        """
        from astrbot_plugin_faith_ladder.db_manager import BEIJING_TZ
        last_run_date = None
        while self._running:
            try:
                now_bj = datetime.now(BEIJING_TZ)
                today = now_bj.strftime("%Y-%m-%d")
                # 除了进程内的记忆，还要看已有备份文件名：插件重启（或当天重启多次）
                # 时不应把当天的备份与清理重跑一遍
                if last_run_date != today and self._latest_backup_date() == now_bj.strftime("%Y%m%d"):
                    last_run_date = today
                if last_run_date == today:
                    await asyncio.sleep(600)
                    continue

                config = self._get_config()
                if cfg_get(config, "auto_backup_enabled"):
                    await self._do_backup(config)

                # Purge old score history
                if self._purge_score_history:
                    retention_days = cfg_get(config, "score_history_retention_days")
                    try:
                        deleted = await self._purge_score_history(retention_days)
                        if deleted > 0:
                            logger.info(f"Purged {deleted} old score history entries (>{retention_days} days)")
                    except Exception as e:
                        logger.error(f"Score history purge error: {e}")

                # Purge per-day tables (gift accept counts / prayer hits)
                # 这两张表此前从未清理：每次接受道具一行、每人每天一行，会无限增长
                if self._purge_daily_tables:
                    try:
                        deleted = await self._purge_daily_tables(90)
                        if deleted > 0:
                            logger.info(f"Purged {deleted} old daily-state rows (>90 days)")
                    except Exception as e:
                        logger.error(f"Daily tables purge error: {e}")

                # Purge expired statuses
                if self._purge_expired_statuses:
                    try:
                        deleted = await self._purge_expired_statuses()
                        if deleted > 0:
                            logger.info(f"Purged {deleted} expired player statuses")
                    except Exception as e:
                        logger.error(f"Status purge error: {e}")

                # 执行成功才记日期：中途失败会在下个周期（10 分钟后）重试，
                # 而不是把当天的备份与清理整个跳过
                last_run_date = today
                await asyncio.sleep(600)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"SchedulerService backup error: {e}")
                await asyncio.sleep(600)

    async def _gift_cleanup_loop(self):
        """Loop that cleans up expired pending gifts every 60 seconds."""
        while self._running:
            try:
                if self._cleanup_expired_gifts:
                    try:
                        refunded = await self._cleanup_expired_gifts(
                            notify=self._notify_gift_timeout,
                            on_refunded=self._on_gift_refunded,
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

    def _latest_backup_date(self) -> Optional[str]:
        """已有备份文件中最新的一份是哪一天（YYYYMMDD，北京日期）；没有则 None。

        用于判断"今天是否已经备份过"——仅靠进程内变量的话，当天重启会重跑一次。
        文件名形如 ladder_backup_20260913_120000.db（或带 _2 这类同秒后缀）。
        """
        import re

        dates = []
        try:
            for f in self.backup_dir.glob("ladder_backup_*.db"):
                m = re.match(r"ladder_backup_(\d{8})_\d{6}", f.name)
                if m:
                    dates.append(m.group(1))
        except OSError:
            return None
        return max(dates) if dates else None

    async def _do_backup(self, config: dict):
        """生成备份并清理过期备份。

        备份内容交给 backup_db 回调（DB 层用 VACUUM INTO 出一致性快照），
        这里只负责命名、记录日志与按保留天数清理旧文件。
        """
        # 保留天数下限为 1：配置成 0 会让截止时间落在"现在"，把刚生成的备份也删掉
        retention_days = max(1, int(cfg_get(config, "backup_retention_days") or 1))
        if not self._backup_db:
            return

        backup_dir = self.backup_dir
        await asyncio.to_thread(backup_dir.mkdir, parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        # 同一秒内跑两次会撞名（backup_to 会先删同名文件，等于静默覆盖），加计数区分
        backup_path = backup_dir / f"ladder_backup_{timestamp}.db"
        suffix = 1
        while backup_path.exists():
            backup_path = backup_dir / f"ladder_backup_{timestamp}_{suffix}.db"
            suffix += 1

        try:
            await self._backup_db(backup_path)
            logger.info(f"Backup created: {backup_path}")
        except Exception as e:
            # 备份失败不应影响其它定时任务，也不该留下半成品文件
            logger.error(f"Backup failed: {e}")
            if backup_path.exists():
                try:
                    backup_path.unlink()
                except OSError:
                    pass

        # Clean old backups (non-blocking)
        cutoff = datetime.now().timestamp() - (retention_days * 86400)

        def _remove_old():
            """删除超过保留期的备份文件。作为线程函数传给 asyncio.to_thread，避免阻塞事件循环。"""
            for f in backup_dir.glob("ladder_backup_*.db"):
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    logger.info(f"Old backup removed: {f}")

        await asyncio.to_thread(_remove_old)
