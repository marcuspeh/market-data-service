"""APScheduler-driven constituents refresh.

Schedules a daily job that runs at 8:30 AM America/New_York — every
day, regardless of whether it's a US trading day. The job refreshes
every supported ETF via :meth:`ConstituentsService.refresh_all`.

Designed to be started/stopped alongside the FastAPI lifespan.
"""
import logging
from datetime import datetime, time, timedelta

try:
    from zoneinfo import ZoneInfo  # type: ignore[import-not-found]

    NY_TZ = ZoneInfo("America/New_York")
except ImportError:  # pragma: no cover — only on Python <3.9
    from datetime import timezone

    NY_TZ = timezone(timedelta(hours=-5), name="America/New_York")

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

from app.services.constituents_service import ConstituentsService

logger = logging.getLogger(__name__)


class ConstituentsScheduler:
    """Wraps an :class:`AsyncIOScheduler` configured for constituents refresh."""

    def __init__(self, service: ConstituentsService | None = None) -> None:
        self._service = service or ConstituentsService()
        self._scheduler = AsyncIOScheduler(timezone=str(NY_TZ))
        self._started = False

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Start the scheduler and queue the next refresh."""
        if self._started:
            return
        self._started = True
        self._scheduler.start()
        self._schedule_next_run()
        logger.info("Constituents scheduler started")

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        self._scheduler.shutdown(wait=False)
        logger.info("Constituents scheduler stopped")

    # ------------------------------------------------------------------ scheduling

    def _schedule_next_run(self) -> None:
        """Schedule the next refresh for 8:30 AM New York tomorrow."""
        now_ny = datetime.now(NY_TZ)
        # Always target tomorrow at 8:30 ET — never today, even if 8:30
        # hasn't passed yet, because the upstream provider may already
        # have served the data earlier and re-running just wastes quota.
        tomorrow = (now_ny + timedelta(days=1)).date()
        run_at = datetime.combine(tomorrow, time(8, 30), tzinfo=NY_TZ)

        self._scheduler.add_job(
            self._refresh_and_reschedule,
            trigger=DateTrigger(run_date=run_at),
            id="constituents_refresh",
            replace_existing=True,
            misfire_grace_time=300,  # tolerate up to 5min late start
        )
        logger.info(f"Next constituents refresh scheduled at {run_at.isoformat()}")

    async def _refresh_and_reschedule(self) -> None:
        """Refresh all tickers, then schedule the next run."""
        snap_date = datetime.now(NY_TZ).date()
        try:
            results = await self._service.refresh_all(snap_date)
            logger.info(f"Scheduled refresh complete for {snap_date}: {results}")
        except Exception as e:  # noqa: BLE001
            logger.error(f"Scheduled refresh failed for {snap_date}: {e}")
        finally:
            self._schedule_next_run()