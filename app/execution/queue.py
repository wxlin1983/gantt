"""PostgreSQL-backed job queue (implement.md §3.8, §6.3).

A table rather than a broker. Keeping the queue in the same transaction as task
state removes a whole class of "the job says done, the row says running"
inconsistencies, and is one fewer service to operate. Multiple workers claim
work safely with ``SELECT ... FOR UPDATE SKIP LOCKED``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import JobQueue, JobType
from app.services.cases import now_utc

#: A claim older than this is assumed to belong to a worker that died.
LOCK_TIMEOUT = timedelta(minutes=5)

#: Retry ceiling for the queue mechanism itself, distinct from a task's own
#: api_retry_max. This one guards against a job that always explodes.
MAX_JOB_ATTEMPTS = 5


async def enqueue(
    session: AsyncSession,
    job_type: JobType,
    *,
    case_id: int | None = None,
    case_task_id: int | None = None,
    payload: dict[str, Any] | None = None,
    run_after: datetime | None = None,
    dedupe: bool = True,
) -> JobQueue | None:
    """Add a job, optionally skipping if an equivalent one is already waiting.

    Deduplication matters for triggers: the periodic sweep exists to catch
    missed work, and without it that sweep would double-fire everything.
    """
    if dedupe and case_task_id is not None:
        existing = (
            await session.scalars(
                select(JobQueue.id).where(
                    JobQueue.job_type == job_type,
                    JobQueue.case_task_id == case_task_id,
                )
            )
        ).first()
        if existing is not None:
            return None

    job = JobQueue(
        job_type=job_type,
        case_id=case_id,
        case_task_id=case_task_id,
        payload=payload or {},
        run_after=run_after or now_utc(),
    )
    session.add(job)
    await session.flush()
    return job


async def claim(
    session: AsyncSession, worker_id: str, limit: int = 10
) -> list[JobQueue]:
    """Take up to ``limit`` due jobs for this worker.

    ``SKIP LOCKED`` is what makes several workers safe against one table. On
    SQLite the clause is a no-op, which is fine for the test suite because it
    runs a single worker.
    """
    await reclaim_stale(session)

    statement = (
        select(JobQueue)
        .where(
            JobQueue.locked_by.is_(None),
            JobQueue.run_after <= now_utc(),
        )
        .order_by(JobQueue.run_after)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    jobs = list((await session.scalars(statement)).all())
    if not jobs:
        return []

    moment = now_utc()
    for job in jobs:
        job.locked_by = worker_id
        job.locked_at = moment
    await session.flush()
    return jobs


async def reclaim_stale(session: AsyncSession) -> int:
    """Release claims from workers that never came back."""
    cutoff = now_utc() - LOCK_TIMEOUT
    result = await session.execute(
        update(JobQueue)
        .where(
            JobQueue.locked_by.is_not(None),
            JobQueue.locked_at < cutoff,
        )
        .values(locked_by=None, locked_at=None)
    )
    return result.rowcount or 0


async def complete(session: AsyncSession, job: JobQueue) -> None:
    """Finished jobs are deleted, not marked; the audit trail is elsewhere."""
    await session.execute(delete(JobQueue).where(JobQueue.id == job.id))
    await session.flush()


async def defer(
    session: AsyncSession, job: JobQueue, delay: timedelta
) -> None:
    """Put a job back for a later attempt."""
    job.attempts += 1
    job.locked_by = None
    job.locked_at = None
    job.run_after = now_utc() + delay
    await session.flush()


async def abandon(session: AsyncSession, job: JobQueue) -> None:
    await complete(session, job)


async def pending_count(session: AsyncSession) -> int:
    rows = await session.scalars(select(JobQueue.id))
    return len(list(rows.all()))


async def oldest_age_seconds(session: AsyncSession) -> float:
    """Age of the longest-waiting job, for the queue-backlog metric."""
    oldest = (
        await session.scalars(
            select(JobQueue.run_after).order_by(JobQueue.run_after).limit(1)
        )
    ).first()
    if oldest is None:
        return 0.0
    return max((now_utc() - oldest).total_seconds(), 0.0)
