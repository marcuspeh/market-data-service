"""APScheduler-driven constituents refresh + yesterday's bar backfill.

Runs daily at 8:30 AM America/New_York (every day, regardless of
holidays). The job:

  1. Refreshes every supported ETF's holdings via
     :meth:`ConstituentsService.refresh_all`.
  2. For every ticker that succeeded, also persists **yesterday's**
     daily bar to the parquet cache via
     :meth:`MarketDataService.backfill_yesterday` — by 8:30 ET the
     previous trading day's bar is final and safe to cache once.
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

from app.config.settings import get_settings
from app.services.constituents_service import ConstituentsService
from app.services.market_data_service import MarketDataService

logger = logging.getLogger(__name__)


class ConstituentsScheduler:
    def __init__(
        self,
        service: ConstituentsService | None = None,
        market_data_service: MarketDataService | None = None,
    ) -> None:
        settings = get_settings()
        self._service = service or ConstituentsService()
        self._market_data = market_data_service or MarketDataService(settings)
        self._scheduler = AsyncIOScheduler(timezone=str(NY_TZ))
        self._started = False

    def start(self) -> None:
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

    def _schedule_next_run(self) -> None:
        """Schedule the next run for 8:30 AM New York tomorrow."""
        now_ny = datetime.now(NY_TZ)
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
        snap_date = datetime.now(NY_TZ).date()
        try:
            results = await self._service.refresh_all(snap_date)
            logger.info(
                f"Scheduled constituents refresh complete for {snap_date}: "
                f"{results}"
            )

            for ticker, count in results.items():
                if count < 1:
                    # Refresh failed for this ticker (count == -1); skip.
                    continue
                try:
                    self._market_data.backfill_yesterday(ticker)
                except Exception as e:  # noqa: BLE001
                    logger.error(f"backfill_yesterday failed for {ticker}: {e}")
        except Exception as e:  # noqa: BLE001
            logger.error(f"Scheduled refresh failed for {snap_date}: {e}")
        finally:
            self._schedule_next_run()