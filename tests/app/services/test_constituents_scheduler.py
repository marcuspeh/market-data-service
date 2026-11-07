"""Tests for the ConstituentsScheduler."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

from zoneinfo import ZoneInfo

from app.services.constituents_scheduler import ConstituentsScheduler


NY_TZ = ZoneInfo("America/New_York")


class _FrozenDatetime(datetime):
    """``datetime`` stand-in whose ``now()`` is pinned to a fixed moment.

    Subclassing keeps ``combine()`` and the constructor real, so only
    the clock is faked — patching the whole module would turn
    ``datetime.combine`` into a MagicMock and break the code under test.
    """

    _frozen_now: datetime | None = None

    @classmethod
    def now(cls, tz=None):  # noqa: ANN001 - matches datetime.now signature
        assert cls._frozen_now is not None
        if tz is None:
            return cls._frozen_now.replace(tzinfo=None)
        return cls._frozen_now.astimezone(tz)

    @classmethod
    def freeze(cls, moment: datetime) -> type["_FrozenDatetime"]:
        frozen = type("FrozenDatetime", (_FrozenDatetime,), {"_frozen_now": moment})
        return frozen


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

    def test_schedules_today_when_started_before_8_30(self):
        """A restart before 8:30 ET must still run today.

        Aiming at tomorrow unconditionally would skip the day's
        snapshot whenever the process comes up in the early morning.
        """
        sch = ConstituentsScheduler(service=MagicMock())
        fake_now = datetime(2026, 11, 6, 6, 0, tzinfo=NY_TZ)

        with patch(
            "app.services.constituents_scheduler.datetime",
            _FrozenDatetime.freeze(fake_now),
        ), patch.object(sch._scheduler, "add_job") as mock_add_job:
            sch._schedule_next_run()

        run_at = mock_add_job.call_args.kwargs["trigger"].run_date
        assert run_at == datetime(2026, 11, 6, 8, 30, tzinfo=NY_TZ)
        assert run_at > fake_now

    def test_schedules_tomorrow_when_started_after_8_30(self):
        """After 8:30 ET the next occurrence is tomorrow."""
        sch = ConstituentsScheduler(service=MagicMock())
        fake_now = datetime(2026, 11, 6, 9, 0, tzinfo=NY_TZ)

        with patch(
            "app.services.constituents_scheduler.datetime",
            _FrozenDatetime.freeze(fake_now),
        ), patch.object(sch._scheduler, "add_job") as mock_add_job:
            sch._schedule_next_run()

        run_at = mock_add_job.call_args.kwargs["trigger"].run_date
        assert run_at == datetime(2026, 11, 7, 8, 30, tzinfo=NY_TZ)
        assert run_at > fake_now

    def test_schedules_tomorrow_when_started_exactly_at_8_30(self):
        """Exactly 8:30 ET counts as passed — the next run is tomorrow."""
        sch = ConstituentsScheduler(service=MagicMock())
        fake_now = datetime(2026, 11, 6, 8, 30, tzinfo=NY_TZ)

        with patch(
            "app.services.constituents_scheduler.datetime",
            _FrozenDatetime.freeze(fake_now),
        ), patch.object(sch._scheduler, "add_job") as mock_add_job:
            sch._schedule_next_run()

        run_at = mock_add_job.call_args.kwargs["trigger"].run_date
        assert run_at == datetime(2026, 11, 7, 8, 30, tzinfo=NY_TZ)


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

    async def test_calls_t1_backfill_after_refresh(self):
        """After refresh, the scheduler should ask the service to
        plug any T-1 gap with T-2 data and pass the snapshot date."""
        sch = ConstituentsScheduler(
            service=MagicMock(),
            market_data_service=MagicMock(),
        )
        sch._schedule_next_run = MagicMock()

        async def fake_refresh_all(date_):
            return {"SPY": 10, "QQQ": 5, "IWM": -1}

        sch._service.refresh_all = fake_refresh_all
        sch._service.backfill_missing_t1_from_t2 = MagicMock(
            return_value={"SPY": "backfilled", "QQQ": "already_present", "IWM": "no_t2_data"}
        )
        sch._market_data.backfill_yesterday = MagicMock()

        await sch._refresh_and_reschedule()

        sch._service.backfill_missing_t1_from_t2.assert_called_once()
        sch._schedule_next_run.assert_called_once()

    async def test_t1_backfill_failure_does_not_break_loop(self):
        sch = ConstituentsScheduler(
            service=MagicMock(),
            market_data_service=MagicMock(),
        )
        sch._schedule_next_run = MagicMock()

        async def fake_refresh_all(date_):
            return {"SPY": 1}

        sch._service.refresh_all = fake_refresh_all
        sch._service.backfill_missing_t1_from_t2 = MagicMock(
            side_effect=RuntimeError("backfill boom")
        )
        sch._market_data.backfill_yesterday = MagicMock()

        # Should NOT raise.
        await sch._refresh_and_reschedule()

        # The rest of the pipeline still runs and we still re-arm.
        sch._market_data.backfill_yesterday.assert_called_once_with("SPY")
        sch._schedule_next_run.assert_called_once()