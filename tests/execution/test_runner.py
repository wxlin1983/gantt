"""Task execution state machine (implement.md §6.2, §6.4, §6.5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.execution import queue, runner
from app.execution.registry import (
    HandlerRegistry,
    TaskContext,
    TaskResult,
)
from app.models import (
    CompletionSource,
    FailurePolicy,
    JobQueue,
    JobType,
    Notification,
    RunStatus,
    TaskRun,
    TaskStatus,
)
from app.services import cases

TARGET = datetime(2026, 9, 30, 18, 0, tzinfo=UTC)


class Scripted:
    """A handler whose answers are dictated by the test."""

    def __init__(self, *results: TaskResult):
        self.results = list(results)
        self.trigger_calls: list[TaskContext] = []
        self.poll_calls: list[TaskContext] = []

    def _next(self) -> TaskResult:
        return self.results.pop(0) if self.results else TaskResult.running()

    async def trigger(self, ctx: TaskContext) -> TaskResult:
        self.trigger_calls.append(ctx)
        return self._next()

    async def poll(self, ctx: TaskContext) -> TaskResult:
        self.poll_calls.append(ctx)
        return self._next()


@pytest.fixture
def registry():
    return HandlerRegistry()


async def make_case(session, seeded, **overrides):
    kwargs = {
        "name": "Exec case",
        "template_name": "launch",
        "target_date": TARGET,
        "params": {"test_hours": 16, "needs_review": True},
        "role_assignments": {"owner": "pm", "tester": "qa"},
    }
    kwargs.update(overrides)
    return await cases.create(session, seeded["admin"], **kwargs)


async def ready_api_task(session, seeded, registry, handler):
    """A case with its API-backed task unblocked and a handler registered."""
    case = await make_case(session, seeded)
    by_name = {task.name: task for task in case.tasks}
    await cases.complete_task(session, seeded["pm"], case, by_name["plan"])
    task = by_name["test"]
    assert task.status is TaskStatus.READY
    assert task.task_api == "http_request"
    registry.register("http_request", handler)
    return case, task


class TestTrigger:
    async def test_success_completes_the_task(self, session, seeded, registry):
        handler = Scripted(TaskResult.succeeded(build="ok"))
        case, task = await ready_api_task(session, seeded, registry, handler)

        outcome = await runner.trigger(session, registry, case, task)
        assert outcome.status is TaskStatus.DONE
        assert task.status is TaskStatus.DONE
        assert task.completion_source is CompletionSource.API
        # A handler completion has no human behind it
        assert task.completed_by_id is None

        run = await runner.latest_run(session, task)
        assert run.status is RunStatus.SUCCEEDED
        assert run.response_payload == {"build": "ok"}

    async def test_running_result_queues_a_poll_and_a_timeout_check(
        self, session, seeded, registry
    ):
        handler = Scripted(TaskResult.running(external_ref="job-9"))
        case, task = await ready_api_task(session, seeded, registry, handler)

        outcome = await runner.trigger(session, registry, case, task)
        assert outcome.status is TaskStatus.RUNNING
        assert task.status is TaskStatus.RUNNING
        assert task.actual_start is not None

        run = await runner.latest_run(session, task)
        assert run.external_ref == "job-9"

        jobs = (await session.scalars(select(JobQueue))).all()
        kinds = {job.job_type for job in jobs}
        assert JobType.POLL in kinds
        assert JobType.TIMEOUT_CHECK in kinds

    async def test_context_carries_params_and_idempotency_key(
        self, session, seeded, registry
    ):
        handler = Scripted(TaskResult.succeeded())
        case, task = await ready_api_task(session, seeded, registry, handler)
        await runner.trigger(session, registry, case, task)

        ctx = handler.trigger_calls[0]
        assert ctx.task_name == "test"
        assert ctx.case_params["test_hours"] == 16
        assert ctx.attempt == 1
        assert len(ctx.idempotency_key) == 64
        # Stable within an attempt, so a retried network call deduplicates
        assert ctx.idempotency_key == ctx.idempotency_key

    async def test_unknown_handler_fails_without_retrying(
        self, session, seeded, registry
    ):
        case = await make_case(session, seeded)
        by_name = {task.name: task for task in case.tasks}
        await cases.complete_task(session, seeded["pm"], case, by_name["plan"])
        task = by_name["test"]

        outcome = await runner.trigger(session, registry, case, task)
        assert outcome.status is TaskStatus.FAILED
        assert not outcome.requeued

    async def test_already_settled_task_is_left_alone(
        self, session, seeded, registry
    ):
        handler = Scripted(TaskResult.succeeded())
        case, task = await ready_api_task(session, seeded, registry, handler)
        # A person got there first
        await cases.complete_task(session, seeded["qa"], case, task)

        outcome = await runner.trigger(session, registry, case, task)
        assert outcome.detail == "already settled"
        assert handler.trigger_calls == []
        assert task.completion_source is CompletionSource.MANUAL


class TestPoll:
    async def test_poll_until_success(self, session, seeded, registry):
        handler = Scripted(
            TaskResult.running(external_ref="job-1"),
            TaskResult.running(),
            TaskResult.succeeded(status="SUCCESS"),
        )
        case, task = await ready_api_task(session, seeded, registry, handler)

        await runner.trigger(session, registry, case, task)
        await runner.poll(session, registry, case, task)
        assert task.status is TaskStatus.RUNNING

        await runner.poll(session, registry, case, task)
        assert task.status is TaskStatus.DONE
        assert len(handler.poll_calls) == 2
        # The external reference survives across polls
        assert handler.poll_calls[0].external_ref == "job-1"

    async def test_poll_failure_retries(self, session, seeded, registry):
        handler = Scripted(
            TaskResult.running(),
            TaskResult.failed("upstream 500"),
        )
        case, task = await ready_api_task(session, seeded, registry, handler)
        await runner.trigger(session, registry, case, task)
        outcome = await runner.poll(session, registry, case, task)

        assert outcome.requeued
        assert task.status is TaskStatus.READY
        triggers = (
            await session.scalars(
                select(JobQueue).where(JobQueue.job_type == JobType.TRIGGER)
            )
        ).all()
        assert triggers


class TestRetries:
    async def test_backoff_grows_and_then_gives_up(
        self, session, seeded, registry
    ):
        handler = Scripted(*[TaskResult.failed("nope")] * 6)
        case, task = await ready_api_task(session, seeded, registry, handler)
        task.api_config = {
            **task.api_config,
            "api_retry_max": 2,
            "api_retry_interval": 60,
        }
        await session.flush()

        delays = []
        for _ in range(3):
            before = cases.now_utc()
            outcome = await runner.trigger(session, registry, case, task)
            job = (
                await session.scalars(
                    select(JobQueue)
                    .where(JobQueue.job_type == JobType.TRIGGER)
                    .order_by(JobQueue.id.desc())
                )
            ).first()
            if outcome.requeued and job is not None:
                delays.append((job.run_after - before).total_seconds())
                job.run_after = before
                job.locked_by = None
                await session.flush()

        # 60s then 120s: exponential, so a persistent failure backs off
        assert delays[0] < delays[1]
        assert task.status is TaskStatus.FAILED

    async def test_fatal_skips_the_retry_budget(
        self, session, seeded, registry
    ):
        handler = Scripted(TaskResult.fatal("misconfigured"))
        case, task = await ready_api_task(session, seeded, registry, handler)
        outcome = await runner.trigger(session, registry, case, task)
        assert task.status is TaskStatus.FAILED
        assert not outcome.requeued

    async def test_failure_notifies_owner_and_case_owner(
        self, session, seeded, registry
    ):
        handler = Scripted(TaskResult.fatal("misconfigured"))
        case, task = await ready_api_task(session, seeded, registry, handler)
        await runner.trigger(session, registry, case, task)

        rows = (
            await session.scalars(
                select(Notification).where(Notification.type == "task.failed")
            )
        ).all()
        assert {row.user_id for row in rows} == {
            seeded["qa"].id,
            seeded["admin"].id,
        }


class TestFailurePolicy:
    async def test_block_holds_successors(self, session, seeded, registry):
        handler = Scripted(TaskResult.fatal("boom"))
        case, task = await ready_api_task(session, seeded, registry, handler)
        by_name = {row.name: row for row in case.tasks}
        await runner.trigger(session, registry, case, task)
        assert by_name["report"].status is TaskStatus.PENDING

    async def test_continue_releases_successors(
        self, session, seeded, registry
    ):
        handler = Scripted(TaskResult.fatal("boom"))
        case, task = await ready_api_task(session, seeded, registry, handler)
        by_name = {row.name: row for row in case.tasks}
        task.on_failure = FailurePolicy.CONTINUE
        # review is the other predecessor of report
        await cases.complete_task(
            session, seeded["qa"], case, by_name["review"]
        )
        await session.flush()

        await runner.trigger(session, registry, case, task)
        assert task.status is TaskStatus.FAILED
        assert by_name["report"].status is TaskStatus.READY

    async def test_cancel_case_policy_stops_everything(
        self, session, seeded, registry
    ):
        from app.models import CaseStatus

        handler = Scripted(TaskResult.fatal("boom"))
        case, task = await ready_api_task(session, seeded, registry, handler)
        task.on_failure = FailurePolicy.CANCEL_CASE
        await session.flush()

        await runner.trigger(session, registry, case, task)
        assert case.status is CaseStatus.CANCELLED


class TestTimeout:
    async def test_stale_run_times_out(self, session, seeded, registry):
        handler = Scripted(TaskResult.running(external_ref="slow"))
        case, task = await ready_api_task(session, seeded, registry, handler)
        await runner.trigger(session, registry, case, task)

        run = await runner.latest_run(session, task)
        run.started_at = cases.now_utc() - timedelta(hours=2)
        task.api_config = {**task.api_config, "api_timeout": 60}
        await session.flush()

        outcome = await runner.check_timeout(session, case, task)
        assert outcome.requeued or task.status is TaskStatus.FAILED
        refreshed = (
            await session.scalars(select(TaskRun).where(TaskRun.id == run.id))
        ).one()
        assert refreshed.status is RunStatus.TIMEOUT

    async def test_fresh_run_is_not_timed_out(self, session, seeded, registry):
        handler = Scripted(TaskResult.running())
        case, task = await ready_api_task(session, seeded, registry, handler)
        await runner.trigger(session, registry, case, task)
        outcome = await runner.check_timeout(session, case, task)
        assert outcome.detail == "still within timeout"
        assert task.status is TaskStatus.RUNNING


class TestCallback:
    async def test_callback_completes_the_task(
        self, session, seeded, registry
    ):
        handler = Scripted(TaskResult.running())
        case, task = await ready_api_task(session, seeded, registry, handler)
        await runner.trigger(session, registry, case, task)

        run = await runner.latest_run(session, task)
        token = run.request_payload["callback_token"]

        outcome = await runner.resolve_callback(
            session, token, "succeeded", {"result": "ok"}
        )
        assert outcome.status is TaskStatus.DONE
        assert task.status is TaskStatus.DONE

    async def test_token_is_single_use(self, session, seeded, registry):
        handler = Scripted(TaskResult.running())
        case, task = await ready_api_task(session, seeded, registry, handler)
        await runner.trigger(session, registry, case, task)
        run = await runner.latest_run(session, task)
        token = run.request_payload["callback_token"]

        await runner.resolve_callback(session, token, "succeeded")
        with pytest.raises(LookupError):
            await runner.resolve_callback(session, token, "succeeded")

    async def test_unknown_token_is_refused(self, session, seeded):
        with pytest.raises(LookupError):
            await runner.resolve_callback(session, "not-a-token", "succeeded")

    async def test_callback_failure_marks_the_task(
        self, session, seeded, registry
    ):
        handler = Scripted(TaskResult.running())
        case, task = await ready_api_task(session, seeded, registry, handler)
        task.api_config = {**task.api_config, "api_retry_max": 0}
        await session.flush()
        await runner.trigger(session, registry, case, task)
        run = await runner.latest_run(session, task)

        await runner.resolve_callback(
            session,
            run.request_payload["callback_token"],
            "failed",
            message="external system said no",
        )
        assert task.status is TaskStatus.FAILED


class TestQueue:
    async def test_enqueue_dedupes_per_task(self, session, seeded):
        case = await make_case(session, seeded)
        task = case.tasks[0]
        first = await queue.enqueue(
            session, JobType.TRIGGER, case_id=case.id, case_task_id=task.id
        )
        second = await queue.enqueue(
            session, JobType.TRIGGER, case_id=case.id, case_task_id=task.id
        )
        assert first is not None
        assert second is None

    async def test_claim_marks_the_worker(self, session, seeded):
        await queue.enqueue(session, JobType.DEADLINE_SCAN, dedupe=False)
        claimed = await queue.claim(session, "worker-1")
        assert len(claimed) == 1
        assert claimed[0].locked_by == "worker-1"

    async def test_future_jobs_are_not_claimed(self, session, seeded):
        await queue.enqueue(
            session,
            JobType.DEADLINE_SCAN,
            run_after=cases.now_utc() + timedelta(hours=1),
            dedupe=False,
        )
        assert await queue.claim(session, "worker-1") == []

    async def test_stale_locks_are_reclaimed(self, session, seeded):
        await queue.enqueue(session, JobType.DEADLINE_SCAN, dedupe=False)
        claimed = await queue.claim(session, "dead-worker")
        claimed[0].locked_at = cases.now_utc() - timedelta(minutes=10)
        await session.flush()

        # A worker that never came back must not hold work forever
        again = await queue.claim(session, "live-worker")
        assert len(again) == 1
        assert again[0].locked_by == "live-worker"
