"""Tests for the ConstituentsScheduler."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
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
        """The scheduled run date should always be strictly in the future
        from now — never today, even if 8:30 hasn't passed yet."""
        sch = ConstituentsScheduler(service=MagicMock())
        with patch.object(sch._scheduler, "add_job") as mock_add_job:
            sch._schedule_next_run()

        trigger = mock_add_job.call_args.kwargs["trigger"]
        run_at = trigger.run_date
        now = datetime.now(NY_TZ)
        assert run_at > now
        # And at most 48h in the future — protects against a bug that
        # accidentally pushes the run date way out.
        assert (run_at - now).total_seconds() < 48 * 3600