"""Tests for the ConstituentsScheduler."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

from zoneinfo import ZoneInfo

from app.services.constituents_scheduler import ConstituentsScheduler


NY_TZ = ZoneInfo("America/New_York")


class TestSchedulerLifecycle:
    def test_start_then_stop(self):
        sch = ConstituentsScheduler(service=MagicMock())
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
        sch = ConstituentsScheduler(service=MagicMock())
        with patch.object(sch._scheduler, "add_job") as mock_add_job:
            sch._schedule_next_run()
        assert mock_add_job.call_count == 1
        kwargs = mock_add_job.call_args.kwargs
        assert kwargs["id"] == "constituents_refresh"
        assert kwargs["replace_existing"] is True

    def test_runs_at_8_30_new_york(self):
        sch = ConstituentsScheduler(service=MagicMock())
        with patch.object(sch._scheduler, "add_job") as mock_add_job:
            sch._schedule_next_run()

        trigger = mock_add_job.call_args.kwargs["trigger"]
        run_at = trigger.run_date
        assert isinstance(run_at, datetime)
        assert run_at.tzinfo is not None
        # 8:30 AM New York time on the scheduled day.
        local = run_at.astimezone(NY_TZ)
        assert local.hour == 8
        assert local.minute == 30

    def test_run_date_is_in_the_future(self):
        sch = ConstituentsScheduler(service=MagicMock())
        with patch.object(sch._scheduler, "add_job") as mock_add_job:
            sch._schedule_next_run()

        trigger = mock_add_job.call_args.kwargs["trigger"]
        run_at = trigger.run_date
        now = datetime.now(NY_TZ)
        assert run_at > now
        assert (run_at - now).total_seconds() < 48 * 3600


class TestRefreshAndReschedule:
    async def test_calls_refresh_then_backfill_yesterday(self):
        """Verify that after a successful constituents refresh the
        scheduler calls ``backfill_yesterday`` on the market data
        service for every ticker that succeeded."""
        sch = ConstituentsScheduler(
            service=MagicMock(),
            market_data_service=MagicMock(),
        )
        # Pre-stub the inner method so we don't need a real APScheduler.
        sch._schedule_next_run = MagicMock()

        async def fake_refresh_all(date_):
            return {"SPY": 100, "QQQ": 50, "IWM": -1}

        sch._service.refresh_all = fake_refresh_all
        sch._market_data.backfill_yesterday = MagicMock()

        await sch._refresh_and_reschedule()

        # SPY and QQQ succeeded → should backfill. IWM failed → skipped.
        call_args = [c.args[0] for c in sch._market_data.backfill_yesterday.call_args_list]
        assert "SPY" in call_args
        assert "QQQ" in call_args
        assert "IWM" not in call_args

        # Always re-armed.
        sch._schedule_next_run.assert_called_once()

    async def test_backfill_failures_dont_break_refresh_loop(self):
        sch = ConstituentsScheduler(
            service=MagicMock(),
            market_data_service=MagicMock(),
        )
        sch._schedule_next_run = MagicMock()

        async def fake_refresh_all(date_):
            return {"SPY": 1}

        sch._service.refresh_all = fake_refresh_all
        sch._market_data.backfill_yesterday = MagicMock(
            side_effect=RuntimeError("boom")
        )

        # Should NOT raise.
        await sch._refresh_and_reschedule()
        sch._schedule_next_run.assert_called_once()