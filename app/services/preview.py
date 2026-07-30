"""Dry-run scheduling (implement.md §8.3).

Runs the same expansion and backward pass as case creation but writes nothing.
That shared path is the point: the dates shown in the creation wizard, in the
template editor's trial run, and in the case that eventually gets created are
produced by one piece of code and therefore cannot disagree.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.dsl.schema import ExpansionResult
from app.scheduling import (
    Schedule,
    ScheduleEdge,
    backward_pass,
    critical_path,
    forward_pass,
)
from app.services import calendars as calendar_service
from app.services import cases as case_service
from app.services import snapshot


@dataclass(slots=True)
class Preview:
    result: ExpansionResult
    baseline: Schedule
    critical_path: set[str]
    target_date: datetime
    plan_deadline: datetime
    buffer_seconds: int
    calendars: dict[str, Any] = field(default_factory=dict)

    @property
    def earliest_start(self) -> datetime:
        return self.baseline.earliest_start

    @property
    def critical_path_seconds(self) -> int:
        span = self.target_date - self.earliest_start
        return int(span.total_seconds())

    def slack_seconds(self, now: datetime) -> int:
        """How long creation can be deferred and still meet the plan.

        Negative means the plan already had to start in the past.
        """
        return int((self.earliest_start - now).total_seconds())

    def feasible(self, now: datetime) -> bool:
        return self.earliest_start >= now


async def run(
    session: AsyncSession,
    *,
    template_name: str,
    target_date: datetime,
    template_version: int | None = None,
    params: dict[str, Any] | None = None,
    role_assignments: dict[str, str] | None = None,
    case_name: str = "preview",
    now: datetime | None = None,
) -> Preview:
    """Expand and schedule a template without persisting anything."""
    moment = now or case_service.now_utc()
    template_row = await case_service.published_template(
        session, template_name, template_version
    )
    case_snapshot = await snapshot.build(
        session, template_row.definition, template_row.version
    )
    result = await case_service.expand_for(
        session,
        case_snapshot,
        params or {},
        role_assignments or {},
        {"name": case_name, "target_date": target_date.isoformat()},
    )

    registry = calendar_service.from_snapshot(case_snapshot)
    tasks, edges = _engine_input(result)
    baseline = backward_pass(
        tasks,
        edges,
        target_date,
        buffer_seconds=result.buffer_seconds,
        calendars=registry,
    )
    for task in tasks:
        task.baseline_start, task.baseline_end = baseline[task.id]

    # The critical path is a property of the graph, so it is derived from a
    # forecast anchored at the plan itself rather than at "now".
    forecast = forward_pass(tasks, edges, baseline.earliest_start, registry)
    marks = critical_path(tasks, edges, forecast, moment, registry)

    return Preview(
        result=result,
        baseline=baseline,
        critical_path=marks,
        target_date=target_date,
        plan_deadline=target_date
        - timedelta(seconds=result.buffer_seconds),
        buffer_seconds=result.buffer_seconds,
        calendars=registry,
    )


def _engine_input(result: ExpansionResult):
    from app.scheduling import from_expansion

    return from_expansion(result)


@dataclass(slots=True)
class Simulation:
    """What a proposed change would do, without doing it (§8.5)."""

    current_forecast_end: datetime | None
    simulated_forecast_end: datetime | None
    delta_seconds: int
    affected: list[dict[str, Any]]
    exceeds_target: bool
    exceeds_target_by_seconds: int


async def simulate(
    session: AsyncSession,
    case,
    *,
    task_name: str | None = None,
    duration_seconds: int | None = None,
    insert_after: str | None = None,
    insert_duration_seconds: int = 0,
    now: datetime | None = None,
) -> Simulation:
    """Re-forecast with a change applied in memory only.

    This is what lets the drawer say "this makes the report two hours later"
    *before* the user commits. It runs the same forward pass as the real thing,
    so the number shown is the number they will get.
    """
    moment = now or case_service.now_utc()
    prepared = await case_service.build_schedule_input(session, case)
    before = forward_pass(
        prepared.tasks, prepared.edges, moment, prepared.calendars
    )

    tasks = [replace(task) for task in prepared.tasks]
    edges = list(prepared.edges)

    if task_name and duration_seconds is not None:
        for task in tasks:
            if task.id == task_name:
                task.duration_seconds = duration_seconds
                task.duration_days = None

    if insert_after:
        from app.scheduling import ScheduleTask

        placeholder = "__simulated__"
        tasks.append(
            ScheduleTask(
                id=placeholder, duration_seconds=insert_duration_seconds
            )
        )
        # Splice it in: everything that followed the anchor now follows the
        # new step instead, which is what makes the knock-on visible.
        moved = [
            edge for edge in edges if edge.predecessor == insert_after
        ]
        edges = [edge for edge in edges if edge not in moved]
        edges.append(ScheduleEdge(insert_after, placeholder))
        edges.extend(
            ScheduleEdge(placeholder, edge.successor, edge.lag_seconds)
            for edge in moved
        )

    after = forward_pass(tasks, edges, moment, prepared.calendars)

    affected = [
        {
            "name": name,
            "delta_seconds": int(
                (after[name].end - before[name].end).total_seconds()
            ),
        }
        for name in before.intervals
        if name in after
        and after[name].end != before[name].end
    ]
    delta = 0
    if before.latest_end and after.latest_end:
        delta = int((after.latest_end - before.latest_end).total_seconds())
    overshoot = 0
    if after.latest_end:
        overshoot = max(
            int((after.latest_end - case.target_date).total_seconds()), 0
        )

    return Simulation(
        current_forecast_end=before.latest_end,
        simulated_forecast_end=after.latest_end,
        delta_seconds=delta,
        affected=sorted(
            affected, key=lambda item: -abs(item["delta_seconds"])
        ),
        exceeds_target=overshoot > 0,
        exceeds_target_by_seconds=overshoot,
    )
