"""The two scheduling passes (implement.md §5.2 and §5.3).

Pure functions: given tasks, edges, calendars and a reference instant, they
return intervals. Nothing here touches the database, which is what lets case
creation, the dry-run preview and the what-if simulation share one code path
and therefore never disagree.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from app.dsl.graph import topological_sort
from app.models.enums import TaskStatus

from .calendars import CONTINUOUS, Calendar
from .types import (
    Interval,
    Schedule,
    ScheduleEdge,
    ScheduleTask,
    calendar_for,
    edge_maps,
    effective_seconds,
    remaining_seconds,
)


def _default_calendars() -> dict[str, Calendar]:
    return {CONTINUOUS.name: CONTINUOUS}


def _ordered(
    tasks: Sequence[ScheduleTask], edges: Sequence[ScheduleEdge]
) -> list[str]:
    """Topological order, stable in the caller's task order.

    Stability matters: it is what makes two runs over the same inputs produce
    byte-identical schedules.
    """
    return topological_sort(
        [task.id for task in tasks],
        [(edge.predecessor, edge.successor) for edge in edges],
    )


def backward_pass(
    tasks: Sequence[ScheduleTask],
    edges: Sequence[ScheduleEdge],
    target_date: datetime,
    buffer_seconds: int = 0,
    calendars: dict[str, Calendar] | None = None,
) -> Schedule:
    """Derive the baseline by working back from the target date (§5.2).

    Two rules:

    1. A task with no successors ends at ``target_date - buffer``.
    2. Otherwise it ends at the earliest ``successor start - lag``.

    The result is an as-late-as-possible plan, so the critical path carries no
    slack by construction. That is why the project buffer exists (§5.8): it is
    the only reserve in the plan, held at the end where it can be measured.
    """
    calendars = calendars or _default_calendars()
    by_id = {task.id: task for task in tasks}
    outgoing, _ = edge_maps(edges)
    plan_deadline = target_date - timedelta(seconds=buffer_seconds)

    schedule = Schedule()
    for task_id in reversed(_ordered(tasks, edges)):
        task = by_id[task_id]
        calendar = calendar_for(task, calendars)

        successors = outgoing.get(task_id, ())
        if not successors:
            end = plan_deadline
        else:
            # The lag is rewound on the *successor's* calendar: "hand over
            # four hours after this finishes" is four hours of the next
            # person's working time, so a Friday finish waits until Monday.
            end = min(
                calendar_for(by_id[edge.successor], calendars).sub(
                    schedule.start_of(edge.successor), edge.lag_seconds
                )
                for edge in successors
            )

        end = calendar.previous_working_instant(end)
        start = calendar.sub(end, effective_seconds(task, calendar))
        schedule.intervals[task_id] = Interval(start, end)

    return schedule


def forward_pass(
    tasks: Sequence[ScheduleTask],
    edges: Sequence[ScheduleEdge],
    now: datetime,
    calendars: dict[str, Calendar] | None = None,
) -> Schedule:
    """Derive the forecast from actual progress (§5.3).

    Completed tasks contribute their real timestamps; everything else is
    pushed forward from the latest predecessor finish, and never scheduled to
    start in the past.
    """
    calendars = calendars or _default_calendars()
    by_id = {task.id: task for task in tasks}
    _, incoming = edge_maps(edges)

    schedule = Schedule()
    for task_id in _ordered(tasks, edges):
        task = by_id[task_id]
        calendar = calendar_for(task, calendars)

        if task.is_settled:
            # Every settled task is pinned, not just `done` ones. A cancelled
            # task that still occupied its full duration would keep pushing
            # its successors out for work that is never going to happen.
            # Settled without timestamps (cancelled before it began) collapses
            # to a zero-length interval at the present.
            # A task ticked off without ever being started has no start time,
            # and the schedule does not invent one. Crediting it its budgeted
            # duration would put a claim in the UI that nobody made -- "ran
            # 11:32 to 23:32" for work that was simply marked done at 23:32.
            # It collapses to an instant, which the chart draws as a milestone
            # rather than as a bar of no width (design.md §4.6).
            end = task.actual_end or now
            schedule.intervals[task_id] = Interval(
                task.actual_start or end, end
            )
            continue

        predecessors = incoming.get(task_id, ())
        if not predecessors:
            # A task inserted mid-flight has no baseline to respect, so the
            # only floor is the present.
            earliest = max(now, task.baseline_start or now)
        else:
            earliest = max(
                calendar.add(
                    schedule.end_of(edge.predecessor), edge.lag_seconds
                )
                for edge in predecessors
            )

        if task.status is TaskStatus.RUNNING and task.actual_start is not None:
            # The bar begins when work really began -- that is a fact, not a
            # prediction, so it is never snapped forward. What remains of the
            # work happens from now on, which is why the end is measured from
            # `now` rather than from the start.
            start = task.actual_start
            work_begins = max(now, start)
        else:
            start = calendar.next_working_instant(max(earliest, now))
            work_begins = start

        end = calendar.add(
            calendar.next_working_instant(work_begins),
            remaining_seconds(task, calendar, now),
        )
        schedule.intervals[task_id] = Interval(start, end)

    return schedule


def latest_finish(
    tasks: Sequence[ScheduleTask],
    edges: Sequence[ScheduleEdge],
    forecast: Schedule,
    anchor: datetime,
    now: datetime,
    calendars: dict[str, Calendar] | None = None,
) -> Schedule:
    """Latest each task could run without pushing ``anchor`` later.

    Used only to derive total float for the critical path (§5.5). Unlike
    :func:`backward_pass` this runs against the *forecast*, so a task already
    settled is pinned to when it actually happened rather than being given
    imaginary freedom to move.

    The anchor is the case's own forecast finish, not its target date. Floating
    against the target would leave the critical path empty whenever a case is
    comfortably ahead, but the highlight promised in design.md §4.3 -- "delay
    any task on this path and the whole case slips" -- is a property of the
    graph, not of how much room is left before the deadline. How much room is
    left is what buffer consumption reports (§5.8).
    """
    calendars = calendars or _default_calendars()
    by_id = {task.id: task for task in tasks}
    outgoing, _ = edge_maps(edges)

    schedule = Schedule()
    for task_id in reversed(_ordered(tasks, edges)):
        task = by_id[task_id]
        calendar = calendar_for(task, calendars)

        if task.is_settled and task_id in forecast:
            schedule.intervals[task_id] = forecast[task_id]
            continue

        successors = outgoing.get(task_id, ())
        if not successors:
            end = anchor
        else:
            end = min(
                calendar_for(by_id[edge.successor], calendars).sub(
                    schedule.start_of(edge.successor), edge.lag_seconds
                )
                for edge in successors
            )

        end = calendar.previous_working_instant(end)
        start = calendar.sub(end, remaining_seconds(task, calendar, now))
        schedule.intervals[task_id] = Interval(start, end)

    return schedule
