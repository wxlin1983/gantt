"""Driving a single task through its handler (implement.md §6.2, §6.4).

The state machine lives here; the worker loop just decides when to call it.
Keeping them apart means the interesting behaviour -- retries, timeouts, the
race between a manual completion and an API result -- is testable without a
running worker.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dsl.duration import parse_duration
from app.execution import queue
from app.execution.registry import (
    HandlerRegistry,
    Outcome,
    TaskContext,
    TaskResult,
)
from app.models import (
    ApiMode,
    CaseTask,
    CompletionSource,
    FailurePolicy,
    GanttCase,
    JobType,
    RunStatus,
    TaskRun,
    TaskStatus,
)
from app.notifications import service as notifications
from app.services import cases as case_service
from app.services import credentials

#: Retry backoff is capped so a long-lived failure settles into hourly checks
#: rather than drifting to once a week.
MAX_RETRY_DELAY = timedelta(hours=1)

DEFAULT_TIMEOUT_SECONDS = 1800
DEFAULT_POLL_SECONDS = 60
DEFAULT_RETRY_SECONDS = 300


@dataclass(slots=True)
class RunOutcome:
    status: TaskStatus
    detail: str = ""
    requeued: bool = False


def api_setting(task: CaseTask, key: str, default: int) -> int:
    """Read an API timing setting, tolerating either seconds or ``30M``."""
    raw = (task.api_config or {}).get(key)
    if raw is None:
        return default
    if isinstance(raw, int):
        return raw
    try:
        return parse_duration(raw)
    except Exception:  # noqa: BLE001 - fall back rather than break the worker
        return default


async def build_context(
    session: AsyncSession, case: GanttCase, task: CaseTask, attempt: int
) -> TaskContext:
    config = dict(task.api_config or {})
    auth_ref = config.get("auth_ref")
    secrets_map = (
        await credentials.resolve(session, {auth_ref}) if auth_ref else {}
    )
    run = await latest_run(session, task)
    return TaskContext(
        case_id=case.id,
        task_id=task.id,
        task_name=task.name,
        attempt=attempt,
        params=dict(task.params or {}),
        case_params=dict(case.params or {}),
        config=config,
        external_ref=run.external_ref if run else None,
            # `secrets.value` is the single-credential shorthand most configs
        # want; named keys stay available for the rare multi-secret case.
        secrets={"value": next(iter(secrets_map.values()), "")} | secrets_map,
        callback_url=(
            run.request_payload.get("callback_url") if run else None
        ),
    )


async def latest_run(
    session: AsyncSession, task: CaseTask
) -> TaskRun | None:
    return (
        await session.scalars(
            select(TaskRun)
            .where(TaskRun.case_task_id == task.id)
            .order_by(TaskRun.attempt.desc())
            .limit(1)
        )
    ).first()


async def trigger(
    session: AsyncSession,
    registry: HandlerRegistry,
    case: GanttCase,
    task: CaseTask,
) -> RunOutcome:
    """Start a task's handler, creating the attempt record first.

    The run row is written before the call so a worker that dies mid-request
    leaves evidence, and the timeout check can eventually clean it up.
    """
    if task.is_settled or task.status is TaskStatus.RUNNING:
        # Someone completed it by hand, or another worker got there first.
        return RunOutcome(task.status, "already settled")
    if not task.task_api:
        return RunOutcome(task.status, "no handler configured")

    handler = registry.get(task.task_api)
    if handler is None:
        return await _fail(
            session,
            case,
            task,
            None,
            f"handler {task.task_api!r} is not registered",
            retryable=False,
        )

    previous = await latest_run(session, task)
    attempt = (previous.attempt + 1) if previous else 1
    callback_token = secrets.token_urlsafe(32)

    run = TaskRun(
        case_task_id=task.id,
        attempt=attempt,
        handler_name=task.task_api,
        status=RunStatus.RUNNING,
        request_payload={
            "callback_token": callback_token,
            "callback_url": f"/api/v1/callbacks/{callback_token}",
            "config": {
                key: value
                for key, value in (task.api_config or {}).items()
                # Never persist the secret itself, only its name.
                if key != "auth_ref"
            },
        },
    )
    session.add(run)
    task.status = TaskStatus.RUNNING
    task.actual_start = task.actual_start or case_service.now_utc()
    await session.flush()

    ctx = await build_context(session, case, task, attempt)
    result = await handler.trigger(ctx)
    return await _apply(session, case, task, run, result)


async def poll(
    session: AsyncSession,
    registry: HandlerRegistry,
    case: GanttCase,
    task: CaseTask,
) -> RunOutcome:
    """Ask the handler whether work already started has finished."""
    if task.is_settled:
        return RunOutcome(task.status, "already settled")
    handler = registry.get(task.task_api or "")
    run = await latest_run(session, task)
    if handler is None or run is None:
        return RunOutcome(task.status, "nothing to poll")

    ctx = await build_context(session, case, task, run.attempt)
    result = await handler.poll(ctx)
    return await _apply(session, case, task, run, result)


async def check_timeout(
    session: AsyncSession, case: GanttCase, task: CaseTask
) -> RunOutcome:
    """Give up on a run that has been in flight too long."""
    if task.is_settled or task.status is not TaskStatus.RUNNING:
        return RunOutcome(task.status, "not running")
    run = await latest_run(session, task)
    if run is None or run.finished_at is not None:
        return RunOutcome(task.status, "no open run")

    limit = api_setting(task, "api_timeout", DEFAULT_TIMEOUT_SECONDS)
    age = (case_service.now_utc() - run.started_at).total_seconds()
    if age < limit:
        return RunOutcome(task.status, "still within timeout")

    run.status = RunStatus.TIMEOUT
    run.finished_at = case_service.now_utc()
    run.error_message = f"no result after {int(age)}s"
    await session.flush()
    return await _fail(
        session,
        case,
        task,
        run,
        run.error_message,
        retryable=True,
    )


async def _apply(
    session: AsyncSession,
    case: GanttCase,
    task: CaseTask,
    run: TaskRun,
    result: TaskResult,
) -> RunOutcome:
    """Translate a handler result into task and queue state."""
    if result.external_ref:
        run.external_ref = result.external_ref
    if result.payload:
        run.response_payload = result.payload

    match result.outcome:
        case Outcome.RUNNING:
            run.status = RunStatus.RUNNING
            await session.flush()
            if task.api_mode is not ApiMode.TRIGGER_CALLBACK:
                interval = api_setting(
                    task, "api_poll_interval", DEFAULT_POLL_SECONDS
                )
                await queue.enqueue(
                    session,
                    JobType.POLL,
                    case_id=case.id,
                    case_task_id=task.id,
                    run_after=case_service.now_utc()
                    + timedelta(seconds=interval),
                    dedupe=True,
                )
            await queue.enqueue(
                session,
                JobType.TIMEOUT_CHECK,
                case_id=case.id,
                case_task_id=task.id,
                run_after=case_service.now_utc()
                + timedelta(
                    seconds=api_setting(
                        task, "api_timeout", DEFAULT_TIMEOUT_SECONDS
                    )
                ),
                dedupe=True,
            )
            return RunOutcome(TaskStatus.RUNNING, "in flight")

        case Outcome.SUCCEEDED:
            run.status = RunStatus.SUCCEEDED
            run.finished_at = case_service.now_utc()
            await session.flush()
            await case_service.complete_task(
                session,
                actor=None,
                case=case,
                task=task,
                source=CompletionSource.API,
                note="completed by handler",
            )
            return RunOutcome(TaskStatus.DONE, "succeeded")

        case Outcome.FAILED | Outcome.FATAL:
            run.status = RunStatus.FAILED
            run.finished_at = case_service.now_utc()
            run.error_message = result.message
            run.error_detail = result.detail
            await session.flush()
            return await _fail(
                session,
                case,
                task,
                run,
                result.message,
                retryable=result.outcome is Outcome.FAILED,
            )
    return RunOutcome(task.status)


async def _fail(
    session: AsyncSession,
    case: GanttCase,
    task: CaseTask,
    run: TaskRun | None,
    message: str,
    *,
    retryable: bool,
) -> RunOutcome:
    """Retry, or mark failed and apply the task's failure policy."""
    attempt = run.attempt if run else 1
    budget = api_setting(task, "api_retry_max", 3)

    if retryable and attempt <= budget:
        base = api_setting(task, "api_retry_interval", DEFAULT_RETRY_SECONDS)
        # Exponential backoff, capped: a persistent failure should settle into
        # a slow heartbeat rather than hammering or drifting away entirely.
        delay = min(
            timedelta(seconds=base * (2 ** (attempt - 1))), MAX_RETRY_DELAY
        )
        task.status = TaskStatus.READY
        await session.flush()
        await queue.enqueue(
            session,
            JobType.TRIGGER,
            case_id=case.id,
            case_task_id=task.id,
            run_after=case_service.now_utc() + delay,
            dedupe=False,
        )
        return RunOutcome(
            TaskStatus.READY,
            f"retrying in {int(delay.total_seconds())}s",
            requeued=True,
        )

    task.status = TaskStatus.FAILED
    task.version += 1
    await session.flush()

    if task.on_failure is FailurePolicy.CANCEL_CASE:
        await case_service.cancel(
            session, None, case, note=f"{task.name} failed: {message}"
        )
        return RunOutcome(TaskStatus.FAILED, "case cancelled")

    # `continue` means the task is settled despite failing, so downstream work
    # is released here just as a completion would.
    edges = await case_service.dependencies(session, case.id)
    case_service.promote_ready(case.tasks, edges)
    await case_service.recalculate(session, case)

    title, body = notifications.describe(
        notifications.NotificationType.TASK_FAILED,
        task_name=task.display_name or task.name,
        case_name=case.name,
        error=message,
    )
    await notifications.notify(
        session,
        user_ids=[task.owner_id, case.owner_id],
        notification_type=notifications.NotificationType.TASK_FAILED,
        title=title,
        body=body,
        case=case,
        task=task,
        epoch=attempt,
    )
    await case_service.audit(
        session,
        case=case,
        task=task,
        event_type="task.api_failed",
        after_state={"attempt": attempt, "message": message},
    )
    return RunOutcome(TaskStatus.FAILED, message)


async def resolve_callback(
    session: AsyncSession,
    token: str,
    outcome: str,
    payload: dict[str, Any] | None = None,
    message: str = "",
) -> RunOutcome:
    """Apply a result posted back by an external system (§6.5).

    The token is single use and bound to one run: it is a bearer credential
    handed to a third party, so it must not double as a general API key.
    """
    runs = (
        await session.scalars(
            select(TaskRun)
            .where(TaskRun.status == RunStatus.RUNNING)
            .order_by(TaskRun.id.desc())
        )
    ).all()

    run = next(
        (
            candidate
            for candidate in runs
            if (candidate.request_payload or {}).get("callback_token") == token
        ),
        None,
    )
    if run is None:
        raise LookupError("callback token is unknown, used or expired")

    task = (
        await session.scalars(
            select(CaseTask).where(CaseTask.id == run.case_task_id)
        )
    ).one()
    case = await case_service.load(session, task.case_id, for_update=True)
    task = await case_service.find_task(session, case, task.id)

    # Burn the token before acting, so a duplicate POST cannot replay it.
    run.request_payload = {
        **(run.request_payload or {}),
        "callback_token": None,
    }
    await session.flush()

    result = (
        TaskResult.succeeded(**(payload or {}))
        if outcome == "succeeded"
        else TaskResult.failed(message or "external system reported failure")
    )
    return await _apply(session, case, task, run, result)
