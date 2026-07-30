"""Periodic sweeps (implement.md §6.6, §4.16).

Read-mostly work that does not belong to any single task: deadline alerts,
recurring case creation, and the safety net that catches tasks the event path
missed. None of these hold a case lock, because the state table is the source
of truth and the queue is only an accelerator.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.execution import queue
from app.models import (
    CaseStatus,
    CaseTask,
    GanttCase,
    JobType,
    TaskStatus,
)
from app.notifications import service as notifications
from app.notifications.service import NotificationType
from app.services import identity
from app.services.cases import now_utc


async def deadline_scan(session: AsyncSession) -> dict[str, int]:
    """Alert on work that is due, late to start, or late to finish.

    "Late to start" is the only one of the three that is actually actionable:
    by the time a task's *end* is overdue there is nothing left to decide. It
    is also the cheapest to detect, since the data has been sitting there all
    along.
    """
    moment = now_utc()
    rows = (
        await session.execute(
            select(CaseTask, GanttCase)
            .join(GanttCase, GanttCase.id == CaseTask.case_id)
            .where(
                GanttCase.status == CaseStatus.ACTIVE,
                CaseTask.status.in_([TaskStatus.READY, TaskStatus.RUNNING]),
            )
        )
    ).all()

    counts = {"due_soon": 0, "late_start": 0, "overdue": 0, "unassigned": 0}
    for task, case in rows:
        recipients = [task.owner_id, case.owner_id]

        if task.owner_id is None:
            # Nobody to chase, so the group lead is told instead.
            leads = await _group_leads(session, task)
            await _send(
                session,
                NotificationType.TASK_UNASSIGNED,
                task,
                case,
                leads,
            )
            counts["unassigned"] += 1

        if task.baseline_end is not None and moment > task.baseline_end:
            await _send(
                session, NotificationType.TASK_OVERDUE, task, case, recipients
            )
            counts["overdue"] += 1
        elif (
            task.baseline_end is not None
            and moment
            >= task.baseline_end
            - timedelta(seconds=task.warn_before_seconds)
        ):
            await _send(
                session,
                NotificationType.TASK_DUE_SOON,
                task,
                case,
                [task.owner_id],
            )
            counts["due_soon"] += 1

        # `is_unplanned` tasks have no baseline to be late against (§5.10).
        if (
            task.status is TaskStatus.READY
            and task.baseline_start is not None
            and moment > task.baseline_start
        ):
            leads = await _group_leads(session, task)
            await _send(
                session,
                NotificationType.TASK_LATE_START,
                task,
                case,
                [task.owner_id, *leads],
            )
            counts["late_start"] += 1

    return counts


async def _group_leads(
    session: AsyncSession, task: CaseTask
) -> list[int]:
    if task.group_id is None:
        return []
    from app.models import Group

    name = (
        await session.scalars(
            select(Group.name).where(Group.id == task.group_id)
        )
    ).first()
    if name is None:
        return []
    leads = await identity.group_leads(session, {name})
    return list(leads.values())


async def _send(
    session: AsyncSession,
    notification_type: str,
    task: CaseTask,
    case: GanttCase,
    recipients: list[int | None],
) -> None:
    title, body = notifications.describe(
        notification_type,
        task_name=task.display_name or task.name,
        case_name=case.name,
    )
    await notifications.notify(
        session,
        user_ids=[uid for uid in recipients if uid],
        notification_type=notification_type,
        title=title,
        body=body,
        case=case,
        task=task,
        # The task's version acts as the alert epoch, so editing or reopening a
        # task legitimately re-opens its alert cycle.
        epoch=task.version,
    )


async def sweep_ready_tasks(session: AsyncSession) -> int:
    """Queue triggers for API tasks the event path missed (§6.3).

    The state table is authoritative and the queue is a cache of intent; this
    reconciles the two so a dropped enqueue self-heals rather than stalling a
    case forever.
    """
    rows = (
        await session.execute(
            select(CaseTask.id, CaseTask.case_id)
            .join(GanttCase, GanttCase.id == CaseTask.case_id)
            .where(
                GanttCase.status == CaseStatus.ACTIVE,
                CaseTask.status == TaskStatus.READY,
                CaseTask.task_api.is_not(None),
            )
        )
    ).all()

    queued = 0
    for task_id, case_id in rows:
        job = await queue.enqueue(
            session,
            JobType.TRIGGER,
            case_id=case_id,
            case_task_id=task_id,
            dedupe=True,
        )
        if job is not None:
            queued += 1
    return queued


async def refresh_forecasts(
    session: AsyncSession, limit: int = 100
) -> int:
    """Recalculate active cases so their stored forecast does not go stale.

    Only the stored columns drift; the case detail view reforecasts on read.
    This keeps the list view and the health counters honest.
    """
    from app.services import cases as case_service

    rows = (
        await session.scalars(
            select(GanttCase)
            .where(GanttCase.status == CaseStatus.ACTIVE)
            .options(selectinload(GanttCase.tasks))
            .limit(limit)
        )
    ).unique().all()

    for case in rows:
        await case_service.recalculate(session, case)
    return len(rows)
