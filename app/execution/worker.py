"""The worker process (implement.md §6.3).

Claims jobs, dispatches them, and schedules the periodic sweeps. Every job runs
in its own transaction so one poisonous item cannot take the batch with it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import session_scope
from app.execution import queue, runner, scans
from app.execution.registry import HandlerRegistry, load_builtin_handlers
from app.models import JobQueue, JobType
from app.notifications import service as notifications
from app.services import cases as case_service
from app.services.cases import now_utc

log = logging.getLogger("gantt.worker")

#: How often each sweep runs. Deadline scanning is read-only, so five minutes
#: costs little; forecast refresh touches every active case, so it is hourly.
SWEEP_INTERVALS = {
    JobType.DEADLINE_SCAN: timedelta(minutes=5),
    JobType.SCHEDULE_SCAN: timedelta(minutes=1),
    JobType.RECALC: timedelta(hours=1),
}


async def handle(
    session: AsyncSession, registry: HandlerRegistry, job: JobQueue
) -> str:
    """Dispatch one job. Returns a short description for the log."""
    match job.job_type:
        case JobType.TRIGGER | JobType.POLL | JobType.TIMEOUT_CHECK:
            return await _task_job(session, registry, job)

        case JobType.DEADLINE_SCAN:
            counts = await scans.deadline_scan(session)
            await notifications.flush_deliveries(session)
            await _reschedule(session, job)
            return f"deadline scan {counts}"

        case JobType.SCHEDULE_SCAN:
            from app.services import schedules

            created = await schedules.run_due(session)
            await scans.sweep_ready_tasks(session)
            await _reschedule(session, job)
            return f"schedule scan created {created}"

        case JobType.RECALC:
            if job.case_id is not None:
                case = await case_service.load(
                    session, job.case_id, for_update=True
                )
                await case_service.recalculate(session, case)
                return f"recalculated case {job.case_id}"
            refreshed = await scans.refresh_forecasts(session)
            await _reschedule(session, job)
            return f"refreshed {refreshed} forecasts"

    return f"unknown job type {job.job_type}"


async def _task_job(
    session: AsyncSession, registry: HandlerRegistry, job: JobQueue
) -> str:
    if job.case_id is None or job.case_task_id is None:
        return "job is missing its case or task"

    case = await case_service.load(session, job.case_id, for_update=True)
    task = next(
        (row for row in case.tasks if row.id == job.case_task_id), None
    )
    if task is None:
        return "task no longer exists"

    match job.job_type:
        case JobType.TRIGGER:
            outcome = await runner.trigger(session, registry, case, task)
        case JobType.POLL:
            outcome = await runner.poll(session, registry, case, task)
        case _:
            outcome = await runner.check_timeout(session, case, task)
    return f"{job.job_type} {task.name}: {outcome.status} {outcome.detail}"


async def _reschedule(session: AsyncSession, job: JobQueue) -> None:
    """Queue the next occurrence of a recurring sweep."""
    interval = SWEEP_INTERVALS.get(job.job_type)
    if interval is None:
        return
    await queue.enqueue(
        session,
        job.job_type,
        run_after=now_utc() + interval,
        dedupe=False,
    )


async def ensure_sweeps(session: AsyncSession) -> None:
    """Make sure each recurring sweep has exactly one job outstanding."""
    from sqlalchemy import select

    for job_type in SWEEP_INTERVALS:
        existing = (
            await session.scalars(
                select(JobQueue.id).where(JobQueue.job_type == job_type)
            )
        ).first()
        if existing is None:
            await queue.enqueue(session, job_type, dedupe=False)


async def run_once(
    registry: HandlerRegistry, worker_id: str, limit: int = 10
) -> int:
    """Claim and run one batch. Each job gets its own transaction.

    A handler failure is recorded against the task, not raised: one broken
    integration must not stop every other case from progressing.
    """
    async with session_scope() as session:
        jobs = await queue.claim(session, worker_id, limit)
        claimed = [(job.id, job.job_type) for job in jobs]

    processed = 0
    for job_id, job_type in claimed:
        try:
            async with session_scope() as session:
                from sqlalchemy import select

                job = (
                    await session.scalars(
                        select(JobQueue).where(JobQueue.id == job_id)
                    )
                ).one_or_none()
                if job is None:
                    continue
                detail = await handle(session, registry, job)
                await queue.complete(session, job)
                log.info("job %s (%s): %s", job_id, job_type, detail)
                processed += 1
        except Exception:
            log.exception("job %s (%s) raised", job_id, job_type)
            async with session_scope() as session:
                from sqlalchemy import select

                job = (
                    await session.scalars(
                        select(JobQueue).where(JobQueue.id == job_id)
                    )
                ).one_or_none()
                if job is None:
                    continue
                if job.attempts + 1 >= queue.MAX_JOB_ATTEMPTS:
                    # Repeatedly exploding jobs are dropped rather than
                    # left to block the queue; the task keeps its own state.
                    log.error(
                        "dropping job %s after %s attempts",
                        job_id,
                        job.attempts,
                    )
                    await queue.abandon(session, job)
                else:
                    await queue.defer(
                        session, job, timedelta(minutes=2 ** job.attempts)
                    )
    return processed


async def serve(stop: asyncio.Event | None = None) -> None:
    """Run until told to stop."""
    settings = get_settings()
    registry = load_builtin_handlers()
    stop = stop or asyncio.Event()
    interval = settings.worker_poll_interval_ms / 1000

    async with session_scope() as session:
        await ensure_sweeps(session)

    log.info(
        "worker %s started with handlers: %s",
        settings.worker_id,
        ", ".join(registry.names()),
    )
    while not stop.is_set():
        try:
            processed = await run_once(registry, settings.worker_id)
        except Exception:
            log.exception("worker loop failed; continuing")
            processed = 0
        if processed == 0:
            # Nothing to do: wait rather than spin, but stay responsive to the
            # stop signal.
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                pass


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(serve())


if __name__ == "__main__":
    main()
