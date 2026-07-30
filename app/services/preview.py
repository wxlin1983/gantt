"""Dry-run scheduling (implement.md §8.3).

Runs the same expansion and backward pass as case creation but writes nothing.
That shared path is the point: the dates shown in the creation wizard, in the
template editor's trial run, and in the case that eventually gets created are
produced by one piece of code and therefore cannot disagree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.dsl.schema import ExpansionResult
from app.scheduling import (
    Schedule,
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
