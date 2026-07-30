"""Case lifecycle (implement.md §5.7, §8.2, §8.4).

Every mutation that can move dates runs the same shape: lock the case, apply
the change, recalculate the whole forecast, write audit. Full recalculation is
deliberate -- a case holds tens of tasks, and an incremental algorithm would
cost far more in subtle bugs than it saves in milliseconds.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.dsl.errors import DslError
from app.dsl.expansion import expand
from app.dsl.schema import ExpansionResult
from app.models import (
    AuditEvent,
    CaseHealth,
    CaseStatus,
    CaseTask,
    CompletionSource,
    GanttCase,
    GanttTemplateRecord,
    TaskDependency,
    TaskStatus,
    TemplateStatus,
    User,
)
from app.scheduling import (
    Outlook,
    ScheduleEdge,
    ScheduleTask,
    backward_pass,
    evaluate,
    forward_pass,
)
from app.services import calendars as calendar_service
from app.services import identity, snapshot


class CaseError(Exception):
    """A case operation could not be completed."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def now_utc() -> datetime:
    return datetime.now(tz=UTC)


# --- reading ---------------------------------------------------------------


async def load(
    session: AsyncSession, case_id: int, *, for_update: bool = False
) -> GanttCase:
    """Fetch a case with its tasks.

    ``for_update`` takes the row lock that serialises concurrent edits (§5.7).
    Every mutating path must use it.
    """
    statement = (
        select(GanttCase)
        .where(GanttCase.id == case_id)
        .options(selectinload(GanttCase.tasks))
    )
    if for_update:
        # SQLite ignores this; PostgreSQL serialises writers on the case row.
        statement = statement.with_for_update(of=GanttCase)
    case = (await session.scalars(statement)).unique().one_or_none()
    if case is None:
        raise CaseError("E_CASE_NOT_FOUND", f"case {case_id} does not exist")
    return case


async def dependencies(
    session: AsyncSession, case_id: int
) -> list[TaskDependency]:
    rows = await session.scalars(
        select(TaskDependency).where(TaskDependency.case_id == case_id)
    )
    return list(rows.all())


async def find_task(
    session: AsyncSession, case: GanttCase, task_id: int
) -> CaseTask:
    for task in case.tasks:
        if task.id == task_id:
            return task
    raise CaseError(
        "E_TASK_NOT_FOUND", f"task {task_id} is not part of case {case.id}"
    )


# --- scheduling input ------------------------------------------------------


@dataclass(slots=True)
class ScheduleInput:
    tasks: list[ScheduleTask]
    edges: list[ScheduleEdge]
    calendars: dict[str, Any]
    by_name: dict[str, CaseTask]


def _calendar_name(task: CaseTask, snapshot_calendars: dict) -> str:
    """Which calendar a task uses.

    ``schedule_mode`` is the DSL-level choice; the named calendar only applies
    in business mode, so a continuous task cannot accidentally inherit office
    hours.
    """
    if task.schedule_mode != "business":
        return calendar_service.BUILTIN_CONTINUOUS
    return task.api_config.get("calendar") or calendar_service.DEFAULT_OFFICE[
        "name"
    ]


async def build_schedule_input(
    session: AsyncSession, case: GanttCase
) -> ScheduleInput:
    """Turn stored rows into engine input.

    Calendars come from the case's own snapshot, not the live table, so a case
    is always rescheduled against the definitions it was created with.
    """
    registry = calendar_service.from_snapshot(case.template_snapshot)
    by_id = {task.id: task for task in case.tasks}
    by_name = {task.name: task for task in case.tasks}

    tasks = [
        ScheduleTask(
            id=task.name,
            duration_seconds=task.duration_seconds,
            duration_days=(task.api_config or {}).get("duration_days"),
            calendar=_calendar_name(task, registry),
            status=task.status,
            on_failure=task.on_failure,
            is_optional=task.is_optional,
            baseline_start=task.baseline_start,
            baseline_end=task.baseline_end,
            actual_start=task.actual_start,
            actual_end=task.actual_end,
        )
        for task in case.tasks
    ]
    edges = [
        ScheduleEdge(
            predecessor=by_id[row.predecessor_id].name,
            successor=by_id[row.successor_id].name,
            lag_seconds=row.lag_seconds,
        )
        for row in await dependencies(session, case.id)
        if row.predecessor_id in by_id and row.successor_id in by_id
    ]
    return ScheduleInput(tasks, edges, registry, by_name)


# --- recalculation ---------------------------------------------------------


async def recalculate(
    session: AsyncSession, case: GanttCase, at: datetime | None = None
) -> Outlook:
    """Recompute the forecast and write the cached summary columns (§5.7).

    Baselines are never touched here; only the forecast, the critical path
    marks and the case-level rollups.
    """
    moment = at or now_utc()
    prepared = await build_schedule_input(session, case)

    forecast = forward_pass(
        prepared.tasks, prepared.edges, moment, prepared.calendars
    )
    outlook = evaluate(
        prepared.tasks,
        prepared.edges,
        case.target_date,
        moment,
        buffer_seconds=case.buffer_seconds,
        calendars=prepared.calendars,
        forecast=forecast,
    )

    for name, interval in forecast.intervals.items():
        row = prepared.by_name[name]
        row.forecast_start, row.forecast_end = interval
        row.is_on_critical_path = name in outlook.critical_path

    case.forecast_end = outlook.forecast_end
    case.health = outlook.health
    case.buffer_consumed_ratio = round(outlook.buffer_consumed_ratio, 4)
    case.progress_ratio = round(outlook.progress_ratio, 4)
    case.version += 1
    await session.flush()
    return outlook


def promote_ready(
    tasks: Sequence[CaseTask], edges: Sequence[TaskDependency]
) -> list[CaseTask]:
    """Move tasks between pending and ready as predecessors settle (§6.2).

    The gate is "all predecessors settled", not "all done": a cancelled task,
    or a failed one whose policy is `continue`, also releases its successors.
    Demotion is handled too, so reopening a task re-blocks what followed it.
    """
    by_id = {task.id: task for task in tasks}
    blockers: dict[int, list[CaseTask]] = {task.id: [] for task in tasks}
    for edge in edges:
        if edge.successor_id in blockers and edge.predecessor_id in by_id:
            blockers[edge.successor_id].append(by_id[edge.predecessor_id])

    changed: list[CaseTask] = []
    for task in tasks:
        unblocked = all(
            predecessor.is_settled for predecessor in blockers[task.id]
        )
        if task.status is TaskStatus.PENDING and unblocked:
            task.status = TaskStatus.READY
            changed.append(task)
        elif task.status is TaskStatus.READY and not unblocked:
            task.status = TaskStatus.PENDING
            changed.append(task)
    return changed


# --- creation --------------------------------------------------------------


async def published_template(
    session: AsyncSession, name: str, version: int | None = None
) -> GanttTemplateRecord:
    statement = select(GanttTemplateRecord).where(
        GanttTemplateRecord.name == name,
        GanttTemplateRecord.status == TemplateStatus.PUBLISHED,
    )
    if version is not None:
        statement = statement.where(GanttTemplateRecord.version == version)
    statement = statement.order_by(GanttTemplateRecord.version.desc())
    row = (await session.scalars(statement)).first()
    if row is None:
        wanted = f" v{version}" if version else ""
        raise CaseError(
            "E_TEMPLATE_NOT_FOUND",
            f"no published template named {name!r}{wanted}",
        )
    return row


async def expand_for(
    session: AsyncSession,
    case_snapshot: dict[str, Any],
    params: dict[str, Any],
    roles: dict[str, str],
    case_context: dict[str, Any],
) -> ExpansionResult:
    template, task_templates = snapshot.read(case_snapshot)
    del session
    return expand(
        template,
        task_templates,
        params=params,
        roles=roles,
        case=case_context,
    )


async def create(
    session: AsyncSession,
    actor: User,
    *,
    name: str,
    template_name: str,
    target_date: datetime,
    template_version: int | None = None,
    params: dict[str, Any] | None = None,
    role_assignments: dict[str, str] | None = None,
    idempotency_key: str | None = None,
) -> GanttCase:
    """Create a case from a published template (§8.2).

    The whole operation is one transaction: snapshot, tasks, edges, baseline,
    forecast and audit either all land or none do.
    """
    if idempotency_key:
        existing = (
            await session.scalars(
                select(GanttCase.id).where(
                    GanttCase.idempotency_key == idempotency_key
                )
            )
        ).first()
        if existing is not None:
            # A double submit returns the case that was already created rather
            # than a second copy of it. Reloaded through `load` so its tasks
            # are eagerly present, exactly as on the creating path.
            return await load(session, existing)

    template_row = await published_template(
        session, template_name, template_version
    )
    case_snapshot = await snapshot.build(
        session, template_row.definition, template_row.version
    )

    try:
        result = await expand_for(
            session,
            case_snapshot,
            params or {},
            role_assignments or {},
            {"name": name, "target_date": target_date.isoformat()},
        )
    except DslError as exc:
        raise CaseError(
            exc.issues[0].code if exc.issues else "E_EXPANSION_FAILED",
            "; ".join(str(issue) for issue in exc.issues),
        ) from exc

    case = GanttCase(
        name=name,
        template_name=template_row.name,
        template_version=template_row.version,
        template_snapshot=case_snapshot,
        params=params or {},
        role_assignments=role_assignments or {},
        skipped_tasks=[entry.model_dump() for entry in result.skipped],
        target_date=target_date,
        buffer_seconds=result.buffer_seconds,
        status=CaseStatus.ACTIVE,
        owner_id=actor.id,
        idempotency_key=idempotency_key,
    )
    session.add(case)
    await session.flush()

    await _materialise(session, case, result)
    await _apply_baseline(session, case, result)

    prepared = await build_schedule_input(session, case)
    promote_ready(case.tasks, await dependencies(session, case.id))
    del prepared

    await recalculate(session, case)
    await audit(
        session,
        case=case,
        actor=actor,
        event_type="case.created",
        after_state={
            "template": f"{template_row.name} v{template_row.version}",
            "tasks": len(case.tasks),
            "target_date": target_date.isoformat(),
        },
    )
    return case


async def _materialise(
    session: AsyncSession, case: GanttCase, result: ExpansionResult
) -> None:
    """Write the expanded graph as rows, resolving names to ids."""
    owner_names = {
        task.owner for task in result.tasks if task.owner
    }
    lead_groups = {
        task.owner_source.removeprefix("group_lead:")
        for task in result.tasks
        if task.owner_source.startswith("group_lead:")
    }
    group_names = {task.group for task in result.tasks if task.group}

    users = await identity.users_by_name(session, owner_names)
    groups = await identity.groups_by_name(session, group_names | lead_groups)
    leads = await identity.group_leads(session, lead_groups)

    snapshot_task_templates = case.template_snapshot.get(
        "task_templates", {}
    )

    rows: dict[str, CaseTask] = {}
    for order, task in enumerate(result.tasks):
        source = snapshot_task_templates.get(task.uses, {})
        owner_id = None
        if task.owner and task.owner in users:
            owner_id = users[task.owner].id
        elif task.owner_source.startswith("group_lead:"):
            owner_id = leads.get(
                task.owner_source.removeprefix("group_lead:")
            )

        row = CaseTask(
            case_id=case.id,
            name=task.id,
            display_name=task.label,
            source_task_template=task.uses or None,
            phase=task.phase,
            duration_seconds=task.duration_seconds,
            schedule_mode=task.schedule_mode,
            status=TaskStatus.PENDING,
            owner_id=owner_id,
            owner_source=task.owner_source,
            group_id=groups[task.group].id if task.group in groups else None,
            params=task.params,
            task_api=task.task_api or None,
            api_mode=task.api_mode,
            # duration_days and the calendar name ride along here so the
            # engine can rebuild exactly what expansion decided.
            api_config={
                **(source.get("api_config") or {}),
                "calendar": task.calendar,
                "duration_days": task.duration_days,
            },
            allow_manual_override=task.allow_manual_override,
            on_failure=task.on_failure,
            is_optional=task.optional,
            warn_before_seconds=task.warn_before_seconds,
            sort_order=order,
        )
        session.add(row)
        rows[task.id] = row
    await session.flush()

    for edge in result.edges:
        session.add(
            TaskDependency(
                case_id=case.id,
                predecessor_id=rows[edge.predecessor].id,
                successor_id=rows[edge.successor].id,
                lag_seconds=edge.lag_seconds,
            )
        )
    await session.flush()
    await session.refresh(case, ["tasks"])


async def _apply_baseline(
    session: AsyncSession, case: GanttCase, result: ExpansionResult
) -> None:
    """Run the backward pass once and freeze it as the baseline."""
    prepared = await build_schedule_input(session, case)
    baseline = backward_pass(
        prepared.tasks,
        prepared.edges,
        case.target_date,
        buffer_seconds=result.buffer_seconds,
        calendars=prepared.calendars,
    )
    for name, interval in baseline.intervals.items():
        row = prepared.by_name[name]
        row.baseline_start, row.baseline_end = interval
    await session.flush()


# --- task operations -------------------------------------------------------


async def complete_task(
    session: AsyncSession,
    actor: User | None,
    case: GanttCase,
    task: CaseTask,
    *,
    at: datetime | None = None,
    note: str = "",
    source: CompletionSource = CompletionSource.MANUAL,
) -> Outlook:
    """Mark a task finished and let the consequences propagate (§7.2)."""
    moment = now_utc()
    finished = at or moment
    if finished > moment:
        raise CaseError(
            "E_FUTURE_COMPLETION",
            "completion time cannot be in the future",
        )
    if task.status is TaskStatus.DONE:
        raise CaseError("E_ALREADY_DONE", f"{task.name} is already complete")
    if (
        source is CompletionSource.MANUAL
        and not task.allow_manual_override
        and task.task_api
    ):
        raise CaseError(
            "E_MANUAL_NOT_ALLOWED",
            f"{task.name} must be completed by its API",
        )

    before = task.status
    task.status = TaskStatus.DONE
    task.actual_start = task.actual_start or finished
    task.actual_end = finished
    task.completion_source = source
    # NULL actor means the system acted, not a person.
    task.completed_by_id = actor.id if actor else None
    task.completion_note = note
    task.version += 1
    await session.flush()

    edges = await dependencies(session, case.id)
    promoted = promote_ready(case.tasks, edges)
    outlook = await recalculate(session, case, moment)
    await _close_if_finished(session, case, actor, moment)

    await audit(
        session,
        case=case,
        task=task,
        actor=actor,
        event_type="task.completed",
        before_state={"status": before},
        after_state={
            "status": task.status,
            "actual_end": finished.isoformat(),
            "source": source,
            "promoted": [row.name for row in promoted],
        },
        note=note,
    )
    return outlook


async def _close_if_finished(
    session: AsyncSession,
    case: GanttCase,
    actor: User | None,
    moment: datetime,
) -> None:
    """Complete the case once every required task is settled (§4.12).

    Unfinished optional tasks are cancelled rather than left dangling, and
    their owners are told so it does not look like the work was forgotten.
    """
    required = [task for task in case.tasks if not task.is_optional]
    if not required or not all(task.is_settled for task in required):
        return

    for task in case.tasks:
        if task.is_optional and not task.is_settled:
            task.status = TaskStatus.CANCELLED
            task.actual_end = task.actual_end or moment

    case.status = CaseStatus.COMPLETED
    case.completed_at = moment
    case.health = CaseHealth.ON_TRACK if case.health is None else case.health
    await session.flush()
    await audit(
        session,
        case=case,
        actor=actor,
        event_type="case.completed",
        after_state={"completed_at": moment.isoformat()},
    )


async def update_task(
    session: AsyncSession,
    actor: User,
    case: GanttCase,
    task: CaseTask,
    *,
    duration_seconds: int | None = None,
    owner_id: int | None = None,
    group_id: int | None = None,
    params: dict[str, Any] | None = None,
    display_name: str | None = None,
    expected_version: int | None = None,
) -> Outlook:
    """Edit a task and reforecast.

    Changing the owner marks its source ``manual``, so a later bulk role
    reassignment will not silently undo the choice (§4.10).
    """
    if expected_version is not None and expected_version != task.version:
        raise CaseError(
            "E_STALE_WRITE",
            f"{task.name} was modified by someone else; reload and retry",
        )

    before = {
        "duration_seconds": task.duration_seconds,
        "owner_id": task.owner_id,
        "group_id": task.group_id,
    }
    if duration_seconds is not None:
        if duration_seconds < 0:
            raise CaseError(
                "E_BAD_DURATION", "duration cannot be negative"
            )
        task.duration_seconds = duration_seconds
        # An explicit duration overrides whatever unit the template used.
        task.api_config = {**(task.api_config or {}), "duration_days": None}
    if owner_id is not None:
        task.owner_id = owner_id
        task.owner_source = "manual"
    if group_id is not None:
        task.group_id = group_id
    if params is not None:
        task.params = params
    if display_name is not None:
        task.display_name = display_name
    task.version += 1
    await session.flush()

    outlook = await recalculate(session, case)
    await audit(
        session,
        case=case,
        task=task,
        actor=actor,
        event_type="task.updated",
        before_state=before,
        after_state={
            "duration_seconds": task.duration_seconds,
            "owner_id": task.owner_id,
            "group_id": task.group_id,
        },
    )
    return outlook


async def cancel(
    session: AsyncSession,
    actor: User | None,
    case: GanttCase,
    note: str = "",
) -> None:
    if case.status is not CaseStatus.ACTIVE:
        raise CaseError(
            "E_NOT_ACTIVE", f"case {case.id} is already {case.status}"
        )
    moment = now_utc()
    case.status = CaseStatus.CANCELLED
    for task in case.tasks:
        if not task.is_settled:
            task.status = TaskStatus.CANCELLED
            task.actual_end = task.actual_end or moment
    case.version += 1
    await session.flush()
    await audit(
        session,
        case=case,
        actor=actor,
        event_type="case.cancelled",
        note=note,
    )


async def set_target_date(
    session: AsyncSession,
    actor: User,
    case: GanttCase,
    target_date: datetime,
    note: str = "",
) -> Outlook:
    """Move the target date, keeping the baseline and recording the change.

    The baseline deliberately stays put (§5.10): it is the record of what was
    originally promised, and quietly rewriting it would erase exactly the
    information a post-mortem needs.
    """
    previous = case.target_date
    case.target_date_history = [
        *(case.target_date_history or []),
        {
            "from": previous.isoformat(),
            "to": target_date.isoformat(),
            "by": actor.id,
            "at": now_utc().isoformat(),
            "note": note,
        },
    ]
    case.target_date = target_date
    case.version += 1
    await session.flush()

    outlook = await recalculate(session, case)
    await audit(
        session,
        case=case,
        actor=actor,
        event_type="case.target_date_changed",
        before_state={"target_date": previous.isoformat()},
        after_state={"target_date": target_date.isoformat()},
        note=note,
    )
    return outlook


# --- audit -----------------------------------------------------------------


async def audit(
    session: AsyncSession,
    *,
    case: GanttCase | None = None,
    task: CaseTask | None = None,
    actor: User | None = None,
    event_type: str,
    before_state: dict[str, Any] | None = None,
    after_state: dict[str, Any] | None = None,
    note: str = "",
) -> AuditEvent:
    event = AuditEvent(
        case_id=case.id if case else None,
        case_task_id=task.id if task else None,
        actor_id=actor.id if actor else None,
        event_type=event_type,
        before_state=before_state,
        after_state=after_state,
        note=note,
    )
    session.add(event)
    await session.flush()
    return event
