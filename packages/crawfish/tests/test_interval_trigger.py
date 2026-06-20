"""Interval triggers: sub-minute cadence cron can't express."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from crawfish.triggers import (
    CronSchedule,
    IntervalSchedule,
    IntervalTrigger,
    parse_schedule,
)

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def test_interval_trigger_serialises_to_at_every() -> None:
    assert IntervalTrigger(seconds=30).schedule == "@every 30s"


def test_interval_schedule_fires_every_interval() -> None:
    sched = IntervalSchedule(30)
    assert sched.matches(NOW) is True  # always due; cadence is the inter-cycle sleep
    assert (sched.next_after(NOW) - NOW).total_seconds() == 30


def test_interval_seconds_must_be_positive() -> None:
    with pytest.raises(ValueError):
        IntervalSchedule(0)


def test_parse_schedule_interval_vs_cron() -> None:
    assert isinstance(parse_schedule("@every 30s"), IntervalSchedule)
    assert isinstance(parse_schedule("@every 5m"), IntervalSchedule)
    assert parse_schedule("@every 5m").seconds == 300  # type: ignore[union-attr]
    assert isinstance(parse_schedule("0 8 * * *"), CronSchedule)


def test_parse_schedule_bare_seconds() -> None:
    assert parse_schedule("@every 90").seconds == 90  # type: ignore[union-attr]
