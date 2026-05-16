"""模拟盘调度器：60 秒间隔扫描建仓/平仓。"""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.core.paper_trading_engine import ENGINE

logger = logging.getLogger(__name__)


class PaperTradingScheduler:
    def __init__(self, timezone: str = "UTC", interval_seconds: int = 60):
        self.scheduler = AsyncIOScheduler(timezone=timezone)
        self.interval_seconds = max(15, int(interval_seconds))
        self._running = False

    async def _scan_job(self):
        if self._running:
            logger.debug("[模拟盘] 上轮扫描仍在执行，跳过本轮")
            return
        self._running = True
        try:
            result = await ENGINE.scan_once()
            opened = result.get("opened", 0)
            closed = result.get("closed", 0)
            status = result.get("status", "?")
            # 有实际开/平仓才是业务事件,否则只是心跳。
            level = logging.INFO if (opened or closed) else logging.DEBUG
            logger.log(
                level,
                "[模拟盘] 扫描完成: opened=%s closed=%s status=%s",
                opened,
                closed,
                status,
            )
        except Exception as e:
            logger.exception(f"[模拟盘] 扫描异常: {e}")
        finally:
            self._running = False

    async def _premarket_job(self):
        """盘前计划通知。"""
        try:
            from src.core.paper_trading_notifier import send_premarket_plan
            await send_premarket_plan()
        except Exception as e:
            logger.exception(f"[模拟盘] 盘前计划通知异常: {e}")

    async def _summary_job(self):
        """日终摘要通知。"""
        try:
            from src.core.paper_trading_notifier import send_daily_summary
            await send_daily_summary()
        except Exception as e:
            logger.exception(f"[模拟盘] 日终摘要通知异常: {e}")

    def start(self):
        self.scheduler.add_job(
            self._scan_job,
            "interval",
            seconds=self.interval_seconds,
            id="paper_trading_scan",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        # 盘前计划 - 每天 09:00
        self.scheduler.add_job(
            self._premarket_job,
            "cron",
            hour=9,
            minute=0,
            id="paper_trading_premarket",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        # 日终摘要 - 每天 15:30
        self.scheduler.add_job(
            self._summary_job,
            "cron",
            hour=15,
            minute=30,
            id="paper_trading_summary",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        self.scheduler.start()
        logger.info(f"模拟盘调度器已启动，扫描间隔 {self.interval_seconds}s")

    def shutdown(self):
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:
            pass
        logger.info("模拟盘调度器已关闭")
