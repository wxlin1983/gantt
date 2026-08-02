"""Case endpoints (implement.md §8.1-§8.5)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import selectinload

from app.auth import permissions
from app.models import (
    CaseHealth,
    CaseStatus,
    CaseTask,
    GanttCase,
    Group,
    TaskStatus,
    User,
)
from app.services import cases as case_service
from app.services import preview as preview_service

from ..deps import PrincipalDep, SessionDep, UserDep, require
from ..errors import ApiError
from ..schemas import (
    AuditEventOut,
    CancelRequest,
    CaseDetailOut,
    CaseSummaryOut,
    CompleteTaskRequest,
    CreateCaseRequest,
    DeleteTaskRequest,
    EdgeOut,
    HealthCountsOut,
    InsertTaskRequest,
    MyTaskOut,
    NotificationOut,
    PreviewOut,
    PreviewRequest,
    PreviewTaskOut,
    ResetBaselineRequest,
    SimulateOut,
    SimulateRequest,
    SkippedOut,
    TaskOut,
    TaskRunOut,
    UpdateCaseRequest,
    UpdateTaskRequest,
)

router = APIRouter(tags=["cases"])


def _blocked_on(case: GanttCase) -> list[str]:
    """Tasks the case is currently waiting on.

    Ready and running work is what someone can act on; if there is none but
    the case is still active, a blocking failure is what is holding it.
    """
    actionable = [
        task.display_name or task.name
        for task in case.tasks
        if task.status in (TaskStatus.READY, TaskStatus.RUNNING)
    ]
    if actionable:
        return actionable
    return [
        task.display_name or task.name
        for task in case.tasks
        if task.status is TaskStatus.FAILED and not task.is_settled
    ]


def _overshoot(case: GanttCase) -> int:
    if case.forecast_end is None:
        return 0
    delta = (case.forecast_end - case.target_date).total_seconds()
    return max(int(delta), 0)


def _summary(
    case: GanttCase, people: dict[int, str] | None = None
) -> CaseSummaryOut:
    derived = {"blocked_on", "exceeds_target_by_seconds", "owner_name"}
    return CaseSummaryOut(
        **{
            field: getattr(case, field)
            for field in CaseSummaryOut.model_fields
            if field not in derived
        },
        blocked_on=_blocked_on(case),
        exceeds_target_by_seconds=_overshoot(case),
        # Carried on the row rather than as a lookup table beside it: a list
        # response is a plain array, and one short string per row costs less
        # than wrapping the whole thing in an envelope to hold the map.
        owner_name=(people or {}).get(case.owner_id or -1, ""),
    )


# --- collection ------------------------------------------------------------


@router.post(
    "/cases", response_model=CaseDetailOut, status_code=status.HTTP_201_CREATED
)
async def create_case(
    body: CreateCaseRequest,
    session: SessionDep,
    user: UserDep,
    principal: PrincipalDep,
) -> CaseDetailOut:
    require(
        permissions.can_create_case(principal), "you cannot create cases"
    )
    case = await case_service.create(
        session,
        user,
        name=body.name,
        template_name=body.template_name,
        target_date=_aware(body.target_date),
        template_version=body.template_version,
        params=body.params,
        role_assignments=body.role_assignments,
        idempotency_key=body.idempotency_key,
    )
    return await _detail(session, case, principal)


@router.post("/cases/preview", response_model=PreviewOut)
async def preview_case(
    body: PreviewRequest, session: SessionDep, principal: PrincipalDep
) -> PreviewOut:
    """Schedule a template without creating anything (§8.3)."""
    require(permissions.can_view(principal), "sign in first")
    now = datetime.now(tz=UTC)
    result = await preview_service.run(
        session,
        template_name=body.template_name,
        target_date=_aware(body.target_date),
        template_version=body.template_version,
        params=body.params,
        role_assignments=body.role_assignments,
        case_name=body.name,
        now=now,
    )
    return PreviewOut(
        tasks=[
            PreviewTaskOut(
                name=task.id,
                display_name=task.label,
                phase=task.phase,
                owner=task.owner,
                group=task.group,
                duration_seconds=task.duration_seconds,
                baseline_start=result.baseline.start_of(task.id),
                baseline_end=result.baseline.end_of(task.id),
                is_on_critical_path=task.id in result.critical_path,
                is_optional=task.optional,
            )
            for task in result.result.tasks
        ],
        dependencies=[
            EdgeOut(
                predecessor=edge.predecessor,
                successor=edge.successor,
                lag_seconds=edge.lag_seconds,
            )
            for edge in result.result.edges
        ],
        skipped_tasks=[
            SkippedOut(id=entry.id, label=entry.label, reason=entry.reason)
            for entry in result.result.skipped
        ],
        earliest_start=result.earliest_start,
        plan_deadline=result.plan_deadline,
        target_date=result.target_date,
        buffer_seconds=result.buffer_seconds,
        critical_path_seconds=result.critical_path_seconds,
        critical_path=sorted(result.critical_path),
        feasible=result.feasible(now),
        slack_seconds=result.slack_seconds(now),
        warnings=[
            {
                "code": issue.code,
                "message": issue.message,
                "path": issue.path,
            }
            for issue in result.result.warnings
        ],
    )


@router.get("/cases/summary", response_model=HealthCountsOut)
async def case_counts(
    session: SessionDep, principal: PrincipalDep
) -> HealthCountsOut:
    """The clickable totals above the case list (design.md §8)."""
    require(permissions.can_view(principal), "sign in first")
    rows = (
        await session.execute(
            select(GanttCase.status, GanttCase.health, func.count())
            .where(GanttCase.archived_at.is_(None))
            .group_by(GanttCase.status, GanttCase.health)
        )
    ).all()

    counts = HealthCountsOut()
    for case_status, health, total in rows:
        if case_status is CaseStatus.COMPLETED:
            counts.completed += total
        elif case_status is CaseStatus.CANCELLED:
            counts.cancelled += total
        elif health is CaseHealth.OVERDUE:
            counts.overdue += total
        elif health is CaseHealth.AT_RISK:
            counts.at_risk += total
        else:
            counts.on_track += total
    return counts


@router.get("/cases", response_model=list[CaseSummaryOut])
async def list_cases(
    session: SessionDep,
    principal: PrincipalDep,
    case_status: Annotated[CaseStatus | None, Query(alias="status")] = None,
    health: Annotated[CaseHealth | None, Query()] = None,
    template: Annotated[str | None, Query()] = None,
    owner_id: Annotated[int | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
    include_archived: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[CaseSummaryOut]:
    require(permissions.can_view(principal), "sign in first")

    statement = (
        select(GanttCase)
        .options(selectinload(GanttCase.tasks))
        .limit(limit)
        .offset(offset)
    )
    if not include_archived:
        statement = statement.where(GanttCase.archived_at.is_(None))
    if case_status is not None:
        statement = statement.where(GanttCase.status == case_status)
    if health is not None:
        statement = statement.where(GanttCase.health == health)
    if template is not None:
        statement = statement.where(GanttCase.template_name == template)
    if owner_id is not None:
        statement = statement.where(GanttCase.owner_id == owner_id)
    if q:
        # Case name or any task name: "which cases did a safety review?" is a
        # question people actually ask (design.md §8.3).
        pattern = f"%{q}%"
        statement = statement.where(
            or_(
                GanttCase.name.ilike(pattern),
                GanttCase.id.in_(
                    select(CaseTask.case_id).where(
                        or_(
                            CaseTask.name.ilike(pattern),
                            CaseTask.display_name.ilike(pattern),
                        )
                    )
                ),
            )
        )

    # Worst first: the list should lead with whatever needs attention.
    ordering = {
        CaseHealth.OVERDUE: 0,
        CaseHealth.AT_RISK: 1,
        CaseHealth.ON_TRACK: 2,
    }
    rows = (await session.scalars(statement)).unique().all()
    ranked = sorted(
        rows,
        key=lambda case: (
            ordering.get(case.health, 3),
            case.target_date,
        ),
    )
    owner_ids = {case.owner_id for case in ranked if case.owner_id is not None}
    people = (
        {
            user.id: user.display_name or user.username
            for user in await session.scalars(
                select(User).where(User.id.in_(owner_ids))
            )
        }
        if owner_ids
        else {}
    )
    return [_summary(case, people) for case in ranked]


# --- single case -----------------------------------------------------------


@router.get("/cases/{case_id}", response_model=CaseDetailOut)
async def get_case(
    case_id: int, session: SessionDep, principal: PrincipalDep
) -> CaseDetailOut:
    require(permissions.can_view(principal), "sign in first")
    case = await case_service.load(session, case_id)
    return await _detail(session, case, principal, refresh_forecast=True)


@router.patch("/cases/{case_id}", response_model=CaseDetailOut)
async def update_case(
    case_id: int,
    body: UpdateCaseRequest,
    session: SessionDep,
    user: UserDep,
    principal: PrincipalDep,
) -> CaseDetailOut:
    case = await case_service.load(session, case_id, for_update=True)
    require(
        permissions.can_edit_case(principal, case),
        "only the case owner or an admin can edit this case",
    )
    if body.name is not None:
        case.name = body.name
    if body.target_date is not None:
        await case_service.set_target_date(
            session, user, case, _aware(body.target_date), body.note
        )
    else:
        await case_service.recalculate(session, case)
    return await _detail(session, case, principal)


@router.post("/cases/{case_id}/cancel", response_model=CaseDetailOut)
async def cancel_case(
    case_id: int,
    body: CancelRequest,
    session: SessionDep,
    user: UserDep,
    principal: PrincipalDep,
) -> CaseDetailOut:
    case = await case_service.load(session, case_id, for_update=True)
    require(
        permissions.can_cancel_case(principal, case),
        "only the case owner or an admin can cancel this case",
    )
    await case_service.cancel(session, user, case, body.note)
    return await _detail(session, case, principal)


# --- tasks -----------------------------------------------------------------


@router.patch(
    "/cases/{case_id}/tasks/{task_id}", response_model=CaseDetailOut
)
async def update_task(
    case_id: int,
    task_id: int,
    body: UpdateTaskRequest,
    session: SessionDep,
    user: UserDep,
    principal: PrincipalDep,
) -> CaseDetailOut:
    case = await case_service.load(session, case_id, for_update=True)
    task = await case_service.find_task(session, case, task_id)
    require(
        permissions.can_edit_task(principal, task, case),
        f"you cannot edit {task.name}",
    )
    await case_service.update_task(
        session,
        user,
        case,
        task,
        duration_seconds=body.duration_seconds,
        # `null` means "set to nobody" and absent means "leave alone"; the two
        # are only distinguishable through the fields the client actually sent.
        owner_id=(
            body.owner_id
            if "owner_id" in body.model_fields_set
            else case_service.UNSET
        ),
        group_id=(
            body.group_id
            if "group_id" in body.model_fields_set
            else case_service.UNSET
        ),
        params=body.params,
        display_name=body.display_name,
        expected_version=body.expected_version,
    )
    return await _detail(session, case, principal)


@router.post(
    "/cases/{case_id}/tasks/{task_id}/complete",
    response_model=CaseDetailOut,
)
async def complete_task(
    case_id: int,
    task_id: int,
    body: CompleteTaskRequest,
    session: SessionDep,
    user: UserDep,
    principal: PrincipalDep,
) -> CaseDetailOut:
    case = await case_service.load(session, case_id, for_update=True)
    task = await case_service.find_task(session, case, task_id)
    require(
        permissions.can_complete_task(principal, task, case),
        f"only {task.name}'s owner or their group can complete it",
    )
    await case_service.complete_task(
        session,
        user,
        case,
        task,
        at=_aware(body.at) if body.at else None,
        note=body.note,
    )
    return await _detail(session, case, principal)


@router.post(
    "/cases/{case_id}/tasks/insert",
    response_model=CaseDetailOut,
    status_code=status.HTTP_201_CREATED,
)
async def insert_task(
    case_id: int,
    body: InsertTaskRequest,
    session: SessionDep,
    user: UserDep,
    principal: PrincipalDep,
) -> CaseDetailOut:
    """Add a step to a running case (§8.4).

    The new task has no baseline: it was not in the original plan, and giving
    it one would fabricate a variance figure (§5.10).
    """
    case = await case_service.load(session, case_id, for_update=True)
    neighbours = [
        task
        for task in case.tasks
        if task.name in {*body.predecessors, *body.successors}
    ]
    require(
        permissions.can_insert_task(principal, case, neighbours),
        "you cannot add tasks to this case",
    )
    await case_service.insert_task(
        session,
        user,
        case,
        name=body.name,
        display_name=body.display_name,
        task_template=body.task_template,
        duration_seconds=body.duration_seconds,
        # `null` means "set to nobody" and absent means "leave alone"; the two
        # are only distinguishable through the fields the client actually sent.
        owner_id=(
            body.owner_id
            if "owner_id" in body.model_fields_set
            else case_service.UNSET
        ),
        group_id=(
            body.group_id
            if "group_id" in body.model_fields_set
            else case_service.UNSET
        ),
        params=body.params,
        predecessors=body.predecessors,
        successors=body.successors,
        mode=case_service.InsertMode(body.mode),
    )
    return await _detail(session, case, principal)


@router.post(
    "/cases/{case_id}/tasks/{task_id}/delete", response_model=CaseDetailOut
)
async def delete_task(
    case_id: int,
    task_id: int,
    body: DeleteTaskRequest,
    session: SessionDep,
    user: UserDep,
    principal: PrincipalDep,
) -> CaseDetailOut:
    """Remove a step, stitching its neighbours together by default."""
    case = await case_service.load(session, case_id, for_update=True)
    task = await case_service.find_task(session, case, task_id)
    require(
        permissions.can_delete_task(principal, task, case),
        f"you cannot remove {task.name}",
    )
    await case_service.delete_task(
        session, user, case, task, case_service.DeleteMode(body.mode)
    )
    return await _detail(session, case, principal)


@router.post("/cases/{case_id}/tasks/simulate", response_model=SimulateOut)
async def simulate(
    case_id: int,
    body: SimulateRequest,
    session: SessionDep,
    principal: PrincipalDep,
) -> SimulateOut:
    """Re-forecast a proposed change without applying it (§8.5).

    This is what lets the editor say "this pushes the report two hours out"
    before anything is committed.
    """
    require(permissions.can_view(principal), "sign in first")
    case = await case_service.load(session, case_id)
    result = await preview_service.simulate(
        session,
        case,
        task_name=body.task_name,
        duration_seconds=body.duration_seconds,
        insert_after=body.insert_after,
        insert_duration_seconds=body.insert_duration_seconds,
    )
    return SimulateOut(
        current_forecast_end=result.current_forecast_end,
        simulated_forecast_end=result.simulated_forecast_end,
        delta_seconds=result.delta_seconds,
        affected=result.affected,
        exceeds_target=result.exceeds_target,
        exceeds_target_by_seconds=result.exceeds_target_by_seconds,
    )


@router.post(
    "/cases/{case_id}/tasks/{task_id}/reopen", response_model=CaseDetailOut
)
async def reopen_task(
    case_id: int,
    task_id: int,
    session: SessionDep,
    user: UserDep,
    principal: PrincipalDep,
) -> CaseDetailOut:
    """Undo a completion. Admin only: `done` is otherwise terminal."""
    case = await case_service.load(session, case_id, for_update=True)
    task = await case_service.find_task(session, case, task_id)
    require(
        permissions.can_reopen_task(principal),
        "only a template admin can reopen a completed task",
    )
    await case_service.reopen_task(session, user, case, task)
    return await _detail(session, case, principal)


@router.post(
    "/cases/{case_id}/tasks/{task_id}/retry", response_model=CaseDetailOut
)
async def retry_task(
    case_id: int,
    task_id: int,
    session: SessionDep,
    user: UserDep,
    principal: PrincipalDep,
) -> CaseDetailOut:
    """Queue another attempt at a failed automated task.

    One of the two escape hatches from a failed handler; completing it by hand
    is the other (design.md §7.2).
    """
    from app.execution import queue
    from app.models import JobType

    case = await case_service.load(session, case_id, for_update=True)
    task = await case_service.find_task(session, case, task_id)
    require(
        permissions.can_retry_task(principal, task, case),
        f"you cannot retry {task.name}",
    )
    if not task.task_api:
        raise ApiError(
            "E_NOT_AUTOMATED", f"{task.name} has no handler to retry"
        )

    task.status = TaskStatus.READY
    task.version += 1
    await session.flush()
    await queue.enqueue(
        session,
        JobType.TRIGGER,
        case_id=case.id,
        case_task_id=task.id,
        dedupe=True,
    )
    await case_service.audit(
        session, case=case, task=task, actor=user, event_type="task.retried"
    )
    await case_service.recalculate(session, case)
    return await _detail(session, case, principal)


@router.get(
    "/cases/{case_id}/tasks/{task_id}/runs",
    response_model=list[TaskRunOut],
)
async def task_runs(
    case_id: int,
    task_id: int,
    session: SessionDep,
    principal: PrincipalDep,
) -> list[TaskRunOut]:
    """Every attempt at driving this task, newest first (design.md §7.2)."""
    from app.models import TaskRun

    require(permissions.can_view(principal), "sign in first")
    case = await case_service.load(session, case_id)
    task = await case_service.find_task(session, case, task_id)
    rows = (
        await session.scalars(
            select(TaskRun)
            .where(TaskRun.case_task_id == task.id)
            .order_by(TaskRun.attempt.desc())
        )
    ).all()
    return [TaskRunOut.model_validate(row) for row in rows]


@router.post(
    "/cases/{case_id}/reset-baseline", response_model=CaseDetailOut
)
async def reset_baseline(
    case_id: int,
    body: ResetBaselineRequest,
    session: SessionDep,
    user: UserDep,
    principal: PrincipalDep,
) -> CaseDetailOut:
    """Overwrite the baseline with the current forecast (§5.10).

    Erases what was originally promised, so the previous baseline is archived
    and the whole thing is restricted and audited.
    """
    case = await case_service.load(session, case_id, for_update=True)
    require(
        permissions.can_reset_baseline(principal, case),
        "only the case owner or an admin can reset the baseline",
    )
    await case_service.reset_baseline(session, user, case, body.note)
    return await _detail(session, case, principal)


@router.post("/cases/{case_id}/archive", response_model=CaseDetailOut)
async def archive_case(
    case_id: int,
    session: SessionDep,
    user: UserDep,
    principal: PrincipalDep,
) -> CaseDetailOut:
    """Hide a finished case from the default list without deleting it."""
    case = await case_service.load(session, case_id, for_update=True)
    require(
        permissions.can_edit_case(principal, case),
        "only the case owner or an admin can archive this case",
    )
    case.archived_at = case_service.now_utc()
    await session.flush()
    await case_service.audit(
        session, case=case, actor=user, event_type="case.archived"
    )
    return await _detail(session, case, principal)


@router.get("/cases/{case_id}/audit", response_model=list[AuditEventOut])
async def case_audit(
    case_id: int, session: SessionDep, principal: PrincipalDep
) -> list[AuditEventOut]:
    require(permissions.can_view(principal), "sign in first")
    from app.models import AuditEvent

    rows = (
        await session.scalars(
            select(AuditEvent)
            .where(AuditEvent.case_id == case_id)
            .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        )
    ).all()
    return [AuditEventOut.model_validate(row) for row in rows]


# --- notifications ---------------------------------------------------------


@router.get("/notifications", response_model=list[NotificationOut])
async def list_notifications(
    session: SessionDep,
    user: UserDep,
    unread_only: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[NotificationOut]:
    from app.models import Notification

    statement = (
        select(Notification)
        .where(Notification.user_id == user.id)
        .order_by(Notification.created_at.desc())
        .limit(limit)
    )
    if unread_only:
        statement = statement.where(Notification.read_at.is_(None))
    rows = (await session.scalars(statement)).all()
    return [NotificationOut.model_validate(row) for row in rows]


@router.post(
    "/notifications/{notification_id}/read",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def mark_read(
    notification_id: int, session: SessionDep, user: UserDep
) -> None:
    from app.models import Notification

    row = (
        await session.scalars(
            select(Notification).where(
                Notification.id == notification_id,
                # Scoped to the signed-in user, so an id cannot be used to
                # read or dismiss somebody else's notification.
                Notification.user_id == user.id,
            )
        )
    ).one_or_none()
    if row is None:
        raise ApiError("E_NOT_FOUND", "no such notification")
    row.read_at = case_service.now_utc()
    await session.flush()


@router.post(
    "/notifications/read-all", status_code=status.HTTP_204_NO_CONTENT
)
async def mark_all_read(session: SessionDep, user: UserDep) -> None:
    from sqlalchemy import update

    from app.models import Notification

    await session.execute(
        update(Notification)
        .where(
            Notification.user_id == user.id,
            Notification.read_at.is_(None),
        )
        .values(read_at=case_service.now_utc())
    )


# --- personal --------------------------------------------------------------


@router.get("/my/tasks", response_model=list[MyTaskOut])
async def my_tasks(
    session: SessionDep,
    user: UserDep,
    principal: PrincipalDep,
    include_group: Annotated[bool, Query()] = False,
) -> list[MyTaskOut]:
    """Actionable work for the signed-in user (design.md §10)."""
    conditions = [CaseTask.owner_id == user.id]
    if include_group and principal.group_ids:
        # Group members can act for each other, so their queue is visible too.
        conditions.append(CaseTask.group_id.in_(principal.group_ids))

    rows = (
        await session.execute(
            select(CaseTask, GanttCase.name)
            .join(GanttCase, GanttCase.id == CaseTask.case_id)
            .where(
                GanttCase.status == CaseStatus.ACTIVE,
                CaseTask.status.in_(
                    [TaskStatus.READY, TaskStatus.RUNNING, TaskStatus.PENDING]
                ),
                or_(*conditions),
            )
        )
    ).all()

    now = datetime.now(tz=UTC)
    items = [
        MyTaskOut(
            case_id=task.case_id,
            case_name=case_name,
            task_id=task.id,
            name=task.name,
            display_name=task.display_name,
            status=task.status,
            baseline_start=task.baseline_start,
            baseline_end=task.baseline_end,
            forecast_end=task.forecast_end,
            is_late_start=(
                task.status is TaskStatus.READY
                and task.baseline_start is not None
                and now > task.baseline_start
            ),
        )
        for task, case_name in rows
    ]
    # Late first, then actionable, then merely upcoming.
    urgency = {
        TaskStatus.READY: 0,
        TaskStatus.RUNNING: 1,
        TaskStatus.PENDING: 2,
    }
    items.sort(
        key=lambda item: (
            not item.is_late_start,
            urgency.get(item.status, 3),
            item.baseline_start or datetime.max.replace(tzinfo=UTC),
        )
    )
    return items


# --- helpers ---------------------------------------------------------------


def _aware(moment: datetime) -> datetime:
    """Treat a naive input as UTC rather than rejecting it.

    Clients that send a bare local timestamp are common; the engine requires
    awareness, so the boundary is where the assumption gets made explicit.
    """
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


async def _detail(
    session: SessionDep,
    case: GanttCase,
    principal,
    refresh_forecast: bool = False,
) -> CaseDetailOut:
    """Serialise a case, optionally reforecasting first.

    The stored forecast is only correct as of the last recalculation, so the
    detail view recomputes in memory against the current clock (§5.7). The
    list view deliberately does not: it would mean one pass per row for a
    difference of at most an hour.
    """
    if refresh_forecast and case.status is CaseStatus.ACTIVE:
        await case_service.recalculate(session, case)

    edges = await case_service.dependencies(session, case.id)
    by_id = {task.id: task for task in case.tasks}
    ordered = sorted(
        case.tasks, key=lambda task: (task.sort_order, task.name)
    )

    owner_ids = {task.owner_id for task in case.tasks if task.owner_id}
    owner_ids.discard(None)
    if case.owner_id:
        owner_ids.add(case.owner_id)
    group_ids = {task.group_id for task in case.tasks if task.group_id}

    people = (
        dict(
            (
                await session.execute(
                    select(User.id, User.display_name).where(
                        User.id.in_(owner_ids)
                    )
                )
            ).all()
        )
        if owner_ids
        else {}
    )
    groups = (
        dict(
            (
                await session.execute(
                    select(Group.id, Group.display_name).where(
                        Group.id.in_(group_ids)
                    )
                )
            ).all()
        )
        if group_ids
        else {}
    )

    return CaseDetailOut(
        **_summary(case).model_dump(),
        params=case.params or {},
        role_assignments=case.role_assignments or {},
        buffer_seconds=case.buffer_seconds,
        target_date_history=case.target_date_history or [],
        skipped_tasks=case.skipped_tasks or [],
        version=case.version,
        tasks=[
            TaskOut(
                **{
                    field: getattr(task, field)
                    for field in TaskOut.model_fields
                    if field != "permissions"
                },
                permissions=permissions.task_permissions(
                    principal, task, case
                ),
            )
            for task in ordered
        ],
        dependencies=[
            EdgeOut(
                predecessor=by_id[edge.predecessor_id].name,
                successor=by_id[edge.successor_id].name,
                lag_seconds=edge.lag_seconds,
            )
            for edge in edges
            if edge.predecessor_id in by_id and edge.successor_id in by_id
        ],
        permissions=permissions.case_permissions(principal, case),
        people={str(key): value for key, value in people.items()},
        groups={str(key): value for key, value in groups.items()},
    )


