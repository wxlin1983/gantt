"""Shared helpers for scheduling tests."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.scheduling import (
    ScheduleEdge,
    ScheduleTask,
    office_calendar,
    registry,
)

TW = ZoneInfo("Asia/Taipei")


def at(value: str) -> datetime:
    """Parse a local wall-clock string into an aware datetime."""
    return datetime.fromisoformat(value).replace(tzinfo=TW)


def hhmm(moment: datetime) -> str:
    """Render for comparison against hand-computed expectations."""
    return moment.astimezone(TW).strftime("%Y-%m-%d %H:%M")


def task(task_id: str, hours: float = 12, **kwargs) -> ScheduleTask:
    return ScheduleTask(
        id=task_id, duration_seconds=int(hours * 3600), **kwargs
    )


def chain(*ids: str, lag: int = 0) -> list[ScheduleEdge]:
    return [
        ScheduleEdge(a, b, lag) for a, b in zip(ids, ids[1:], strict=False)
    ]


@pytest.fixture
def office():
    return office_calendar()


@pytest.fixture
def calendars(office):
    return registry(office)
