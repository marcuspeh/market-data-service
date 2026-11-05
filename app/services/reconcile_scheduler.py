"""APScheduler-driven corporate-action reconciliation.

Runs every Saturday at 12:00 PM America/New_York. Bars are cached raw,
so nothing has to be re-based: the job just merges the split/dividend
records Polygon publishes for each ticker's cached window and fills any
trading days missing after the newest cached bar
(:meth:`MarketDataService.reconcile_all`). The adjusted view is rebuilt
on read, so a newly discovered action takes effect on the next request
without rewriting stored prices.

The job runs **in-process**, inside the same FastAPI container that
serves the API (see :mod:`app.main`). It is not a separate cron
container or a host-level crontab entry: the process starts the
scheduler in its lifespan and shuts it down on exit.
"""

from __future__ import annotations

from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo  # type: ignore[import-not-found]

    NY_TZ = ZoneInfo("America/New_York")
except ImportError:  # pragma: no cover — only on Python <3.9
    from datetime import timezone

    NY_TZ = timezone(timedelta(hours=-5), name="America/New_York")

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config.settings import get_settings
from app.logging_setup import client
from app.services.market_data_service import MarketDataService

log = client()


class ReconcileScheduler:
    """Weekly reconciliation of cached historical bars against Polygon."""

    #: Day of week the job fires on (``sat`` = Saturday).
    RUN_DAY_OF_WEEK = "sat"
    #: Local hour the job fires at.
    RUN_HOUR = 12
    #: Local minute the job fires at.
    RUN_MINUTE = 0

    def __init__(self, market_data_service: MarketDataService | None = None) -> None:
        settings = get_settings()
        self._market_data = market_data_service or MarketDataService(settings)
        self._scheduler = AsyncIOScheduler(timezone=str(NY_TZ))
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._scheduler.start()
        self._schedule_next_run()
        log.info(
            "Reconciliation scheduler started (runs %s %02d:%02d %s)",
            self.RUN_DAY_OF_WEEK, self.RUN_HOUR, self.RUN_MINUTE, NY_TZ,
        )

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        self._scheduler.shutdown(wait=False)
        log.info("Reconciliation scheduler stopped")

    def _schedule_next_run(self) -> None:
        """Arm the weekly cron trigger."""
        trigger = CronTrigger(
            day_of_week=self.RUN_DAY_OF_WEEK,
            hour=self.RUN_HOUR,
            minute=self.RUN_MINUTE,
            timezone=NY_TZ,
        )
        self._scheduler.add_job(
            self._run_reconciliation,
            trigger=trigger,
            id="market_data_reconciliation",
            replace_existing=True,
            misfire_grace_time=3600,  # tolerate up to 1h late start
            coalesce=True,
            max_instances=1,
        )
        log.info(
            "Next reconciliation scheduled for %s",
            trigger.get_next_fire_time(None, datetime.now(NY_TZ)),
        )

    async def _run_reconciliation(self) -> None:
        log.info("Starting scheduled historical-bar reconciliation")
        try:
            results = await self._market_data.reconcile_all()
        except Exception as e:  # noqa: BLE001 — a failed run must not kill the scheduler
            log.error("Scheduled reconciliation failed: %s", e)
            return

        for ticker, result in results.items():
            log.info(
                "Reconciliation result for %s: %s", ticker, result.get("status")
            )
        log.info(
            "Scheduled reconciliation complete for %d ticker(s): %s",
            len(results),
            {t: r.get("status") for t, r in results.items()},
        )
