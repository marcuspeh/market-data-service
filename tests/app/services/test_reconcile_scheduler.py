"""Tests for the Saturday reconciliation scheduler."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from zoneinfo import ZoneInfo

from app.services.reconcile_scheduler import ReconcileScheduler

NY_TZ = ZoneInfo("America/New_York")


class TestSchedulerLifecycle:
    def test_start_then_stop(self):
        sch = ReconcileScheduler(market_data_service=MagicMock())
        with patch.object(sch._scheduler, "start") as mock_start, \
             patch.object(sch._scheduler, "shutdown") as mock_shutdown:
            sch.start()
            sch.start()  # second start should be a no-op
            sch.stop()
            sch.stop()  # second stop should also be a no-op

        assert mock_start.call_count == 1
        assert mock_shutdown.call_count == 1


class TestScheduleNextRun:
    def test_schedules_a_job(self):
        sch = ReconcileScheduler(market_data_service=MagicMock())
        with patch.object(sch._scheduler, "add_job") as mock_add_job:
            sch._schedule_next_run()
        assert mock_add_job.call_count == 1
        kwargs = mock_add_job.call_args.kwargs
        assert kwargs["id"] == "market_data_reconciliation"
        assert kwargs["replace_existing"] is True

    def test_runs_saturday_at_noon_new_york(self):
        sch = ReconcileScheduler(market_data_service=MagicMock())
        with patch.object(sch._scheduler, "add_job") as mock_add_job:
            sch._schedule_next_run()

        trigger = mock_add_job.call_args.kwargs["trigger"]
        fire_time = trigger.get_next_fire_time(None, datetime.now(NY_TZ))
        assert fire_time is not None

        local = fire_time.astimezone(NY_TZ)
        assert local.hour == 12
        assert local.minute == 0
        # Saturday == 5 in Python's weekday numbering.
        assert local.weekday() == 5

    def test_fire_time_is_in_the_future(self):
        sch = ReconcileScheduler(market_data_service=MagicMock())
        with patch.object(sch._scheduler, "add_job") as mock_add_job:
            sch._schedule_next_run()

        trigger = mock_add_job.call_args.kwargs["trigger"]
        fire_time = trigger.get_next_fire_time(None, datetime.now(NY_TZ))
        assert fire_time > datetime.now(NY_TZ)
        # Within the next 7 days + a small margin.
        assert (fire_time - datetime.now(NY_TZ)).total_seconds() <= 8 * 24 * 3600


class TestRunReconciliation:
    async def test_calls_reconcile_all(self):
        sch = ReconcileScheduler(market_data_service=MagicMock())
        sch._market_data.reconcile_all = AsyncMock(
            return_value={"AAPL": {"status": "in_sync"}}
        )

        await sch._run_reconciliation()

        sch._market_data.reconcile_all.assert_called_once()

    async def test_failure_does_not_raise(self):
        sch = ReconcileScheduler(market_data_service=MagicMock())
        sch._market_data.reconcile_all = AsyncMock(
            side_effect=RuntimeError("boom")
        )

        # Should NOT raise.
        await sch._run_reconciliation()
