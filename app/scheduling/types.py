"""Input and output types for the scheduling engine.

Deliberately separate from both the DSL schema and the ORM models. The
backward pass runs on freshly expanded template output, the forward pass runs
on live rows with actual timestamps, and neither should have to know about the
other's shape. Adapters build these types from either source.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import NamedTuple

from app.models.enums import FailurePolicy, TaskStatus

from .calendars import CONTINUOUS, Calendar


class Interval(NamedTuple):
    start: datetime
    end: datetime

    @property
    def seconds(self) -> int:
        return int((self.end - self.start).total_seconds())


@dataclass(slots=True)
class ScheduleTask:
    """One task as the engine sees it."""

    id: str
    duration_seconds: int = 0
    #: Set when the duration was written in ``D`` units; resolved against the
    #: task's calendar rather than assumed to be 86400 seconds.
    duration_days: float | None = None
    calendar: str = CONTINUOUS.name

    status: TaskStatus = TaskStatus.PENDING
    on_failure: FailurePolicy = FailurePolicy.BLOCK
    is_optional: bool = False

    #: Null for tasks inserted after the case was created (implement.md §5.10).
    baseline_start: datetime | None = None
    baseline_end: datetime | None = None
    actual_start: datetime | None = None
    actual_end: datetime | None = None

    @property
    def is_settled(self) -> bool:
        """Whether successors may proceed past this task (§4.12)."""
        return self.status.is_settled(self.on_failure)


@dataclass(slots=True, frozen=True)
class ScheduleEdge:
    predecessor: str
    successor: str
    lag_seconds: int = 0


@dataclass(slots=True)
class Schedule:
    """The result of one pass: an interval per task."""

    intervals: dict[str, Interval] = field(default_factory=dict)

    def __contains__(self, task_id: str) -> bool:
        return task_id in self.intervals

    def __getitem__(self, task_id: str) -> Interval:
        return self.intervals[task_id]

    def start_of(self, task_id: str) -> datetime:
        return self.intervals[task_id].start

    def end_of(self, task_id: str) -> datetime:
        return self.intervals[task_id].end

    @property
    def earliest_start(self) -> datetime | None:
        if not self.intervals:
            return None
        return min(interval.start for interval in self.intervals.values())

    @property
    def latest_end(self) -> datetime | None:
        if not self.intervals:
            return None
        return max(interval.end for interval in self.intervals.values())


def calendar_for(
    task: ScheduleTask, calendars: dict[str, Calendar]
) -> Calendar:
    """Look up a task's calendar, defaulting to continuous time.

    An unknown name falls back rather than raising: a case snapshot could in
    principle name a calendar that has since been renamed, and refusing to
    schedule an in-flight case is worse than treating it as 24x7 -- which is
    also the behaviour the DSL default implies.
    """
    return calendars.get(task.calendar, CONTINUOUS)


def effective_seconds(task: ScheduleTask, calendar: Calendar) -> int:
    """The task's duration in working seconds on its own calendar."""
    if task.duration_days is not None:
        return int(task.duration_days * calendar.day_seconds)
    return task.duration_seconds


def remaining_seconds(
    task: ScheduleTask, calendar: Calendar, now: datetime
) -> int:
    """Work still to do, for the forward pass (§5.3).

    A running task is credited with the working time already spent on it, so a
    business-calendar task does not gain a weekend's worth of progress.
    """
    total = effective_seconds(task, calendar)
    if task.status is TaskStatus.RUNNING and task.actual_start is not None:
        spent = calendar.elapsed(task.actual_start, now)
        return max(total - spent, 0)
    return total


def edge_maps(
    edges: Iterable[ScheduleEdge],
) -> tuple[dict[str, list[ScheduleEdge]], dict[str, list[ScheduleEdge]]]:
    """Group edges by predecessor and by successor.

    Both directions are needed hot: the backward pass walks successors, the
    forward pass walks predecessors.
    """
    outgoing: dict[str, list[ScheduleEdge]] = {}
    incoming: dict[str, list[ScheduleEdge]] = {}
    for edge in edges:
        outgoing.setdefault(edge.predecessor, []).append(edge)
        incoming.setdefault(edge.successor, []).append(edge)
    return outgoing, incoming


def shift(moment: datetime, seconds: int) -> datetime:
    return moment + timedelta(seconds=seconds)
