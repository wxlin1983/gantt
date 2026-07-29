"""Critical path, progress and health (implement.md §5.5, §5.8, §5.9)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.models.enums import CaseHealth, FailurePolicy, TaskStatus

from .calendars import Calendar
from .passes import forward_pass, latest_finish
from .types import (
    Schedule,
    ScheduleEdge,
    ScheduleTask,
    calendar_for,
    effective_seconds,
)


def critical_path(
    tasks: Sequence[ScheduleTask],
    edges: Sequence[ScheduleEdge],
    forecast: Schedule,
    now: datetime,
    calendars: dict[str, Calendar] | None = None,
) -> set[str]:
    """The longest path through the graph (§5.5).

    Total float is ``latest_start - earliest_start`` measured against the
    case's own forecast finish; anything at or below zero is critical. Settled
    tasks are excluded because they can no longer delay anything, and optional
    tasks are excluded by definition (§4.12) -- though excluding them from the
    *marking* does not stop their delay propagating downstream.
    """
    anchor = forecast.latest_end
    if anchor is None:
        return set()
    latest = latest_finish(tasks, edges, forecast, anchor, now, calendars)
    critical: set[str] = set()
    for task in tasks:
        if task.is_settled or task.is_optional:
            continue
        if task.id not in forecast or task.id not in latest:
            continue
        float_seconds = (
            latest.start_of(task.id) - forecast.start_of(task.id)
        ).total_seconds()
        if float_seconds <= 0:
            critical.add(task.id)
    return critical


def progress_ratio(
    tasks: Sequence[ScheduleTask],
    now: datetime,
    calendars: dict[str, Calendar] | None = None,
) -> float:
    """Duration-weighted completion (§5.9).

    Counting tasks instead would treat a ten minute notification and a twelve
    hour test run as equal, which badly misrepresents how much work is left.
    Running tasks contribute the working time already spent on them.
    """
    calendars = calendars or {}
    total = 0
    done = 0
    for task in tasks:
        calendar = calendar_for(task, calendars)
        weight = effective_seconds(task, calendar)
        total += weight
        if task.is_settled:
            done += weight
        elif (
            task.status is TaskStatus.RUNNING
            and task.actual_start is not None
        ):
            done += min(calendar.elapsed(task.actual_start, now), weight)
    if total == 0:
        return 1.0 if tasks else 0.0
    return done / total


def late_starts(
    tasks: Sequence[ScheduleTask], now: datetime
) -> list[str]:
    """Tasks that could have started but have not (§6.6).

    The one genuinely actionable warning: once a task's *end* is overdue it is
    already too late to do anything about it.
    """
    return [
        task.id
        for task in tasks
        if task.status is TaskStatus.READY
        and task.baseline_start is not None
        and now > task.baseline_start
    ]


def blocking_failures(tasks: Sequence[ScheduleTask]) -> list[str]:
    return [
        task.id
        for task in tasks
        if task.status is TaskStatus.FAILED
        and task.on_failure is FailurePolicy.BLOCK
    ]


@dataclass(slots=True)
class Outlook:
    """Everything the case list and the case header need."""

    forecast_end: datetime | None
    plan_deadline: datetime
    target_date: datetime
    buffer_seconds: int
    buffer_consumed_ratio: float
    progress_ratio: float
    health: CaseHealth
    critical_path: set[str] = field(default_factory=set)
    late_starts: list[str] = field(default_factory=list)
    blocking_failures: list[str] = field(default_factory=list)

    @property
    def exceeds_target_by_seconds(self) -> int:
        if self.forecast_end is None:
            return 0
        overshoot = (self.forecast_end - self.target_date).total_seconds()
        return max(int(overshoot), 0)

    @property
    def is_complete(self) -> bool:
        return self.progress_ratio >= 1.0


def evaluate(
    tasks: Sequence[ScheduleTask],
    edges: Sequence[ScheduleEdge],
    target_date: datetime,
    now: datetime,
    buffer_seconds: int = 0,
    calendars: dict[str, Calendar] | None = None,
    forecast: Schedule | None = None,
) -> Outlook:
    """Run the forecast and summarise it (§5.8).

    Health answers "should I be worried right now", which is not the same
    question as "is the forecast past the deadline". With a buffer configured,
    burning 70% of it while 30% of the work is done deserves a warning even
    though the forecast still lands inside the target.
    """
    calendars = calendars or {}
    if forecast is None:
        forecast = forward_pass(tasks, edges, now, calendars)

    plan_deadline = target_date - timedelta(seconds=buffer_seconds)
    forecast_end = forecast.latest_end
    progress = progress_ratio(tasks, now, calendars)
    late = late_starts(tasks, now)
    failures = blocking_failures(tasks)

    consumed = _buffer_consumed(forecast_end, plan_deadline, buffer_seconds)
    health = _health(
        forecast_end=forecast_end,
        target_date=target_date,
        buffer_seconds=buffer_seconds,
        consumed=consumed,
        progress=progress,
        troubled=bool(late or failures),
    )

    return Outlook(
        forecast_end=forecast_end,
        plan_deadline=plan_deadline,
        target_date=target_date,
        buffer_seconds=buffer_seconds,
        buffer_consumed_ratio=consumed,
        progress_ratio=progress,
        health=health,
        critical_path=critical_path(
            tasks, edges, forecast, now, calendars
        ),
        late_starts=late,
        blocking_failures=failures,
    )


def _buffer_consumed(
    forecast_end: datetime | None, plan_deadline: datetime, buffer: int
) -> float:
    if forecast_end is None or buffer <= 0:
        return 0.0
    overshoot = (forecast_end - plan_deadline).total_seconds()
    return max(overshoot, 0.0) / buffer


def _health(
    *,
    forecast_end: datetime | None,
    target_date: datetime,
    buffer_seconds: int,
    consumed: float,
    progress: float,
    troubled: bool,
) -> CaseHealth:
    if forecast_end is None:
        return CaseHealth.ON_TRACK

    if buffer_seconds > 0:
        if consumed > 1.0:
            return CaseHealth.OVERDUE
        if consumed > progress or troubled:
            return CaseHealth.AT_RISK
        return CaseHealth.ON_TRACK

    # Without a buffer there is nothing to measure consumption against, so
    # health falls back to the simple comparison (§5.8).
    if forecast_end > target_date:
        return CaseHealth.OVERDUE
    if troubled:
        return CaseHealth.AT_RISK
    return CaseHealth.ON_TRACK
