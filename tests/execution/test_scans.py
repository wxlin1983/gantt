"""Periodic sweeps and notification dedup (§3.9, §6.6)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.execution import scans
from app.models import JobQueue, JobType, Notification, TaskStatus
from app.notifications import service as notifications
from app.notifications.service import NotificationType
from app.services import cases

TARGET = datetime(2026, 9, 30, 18, 0, tzinfo=UTC)


async def make_case(session, seeded):
    return await cases.create(
        session,
        seeded["admin"],
        name="Scan case",
        template_name="launch",
        target_date=TARGET,
        params={"test_hours": 16, "needs_review": True},
        role_assignments={"owner": "pm", "tester": "qa"},
    )


async def notified(session, notification_type: str) -> list[Notification]:
    rows = await session.scalars(
        select(Notification).where(Notification.type == notification_type)
    )
    return list(rows.all())


class TestDeadlineScan:
    async def test_late_start_is_reported(self, session, seeded):
        case = await make_case(session, seeded)
        plan = next(t for t in case.tasks if t.name == "plan")
        plan.baseline_start = cases.now_utc() - timedelta(hours=3)
        plan.baseline_end = cases.now_utc() + timedelta(hours=3)
        await session.flush()

        counts = await scans.deadline_scan(session)
        assert counts["late_start"] == 1
        rows = await notified(session, NotificationType.TASK_LATE_START)
        # The owner and the group lead both hear about it
        assert seeded["pm"].id in {row.user_id for row in rows}

    async def test_overdue_is_reported(self, session, seeded):
        case = await make_case(session, seeded)
        plan = next(t for t in case.tasks if t.name == "plan")
        plan.baseline_end = cases.now_utc() - timedelta(hours=1)
        await session.flush()

        counts = await scans.deadline_scan(session)
        assert counts["overdue"] == 1

    async def test_unassigned_ready_task_escalates_to_the_lead(
        self, session, seeded
    ):
        case = await make_case(session, seeded)
        plan = next(t for t in case.tasks if t.name == "plan")
        plan.owner_id = None
        await session.flush()

        counts = await scans.deadline_scan(session)
        assert counts["unassigned"] == 1
        rows = await notified(session, NotificationType.TASK_UNASSIGNED)
        # rnd's lead is pm
        assert {row.user_id for row in rows} == {seeded["pm"].id}

    async def test_unplanned_task_is_never_late(self, session, seeded):
        case = await make_case(session, seeded)
        plan = next(t for t in case.tasks if t.name == "plan")
        # A task inserted mid-flight has no baseline to be late against
        plan.baseline_start = None
        plan.baseline_end = None
        await session.flush()

        counts = await scans.deadline_scan(session)
        assert counts["late_start"] == 0
        assert counts["overdue"] == 0

    async def test_rescanning_does_not_duplicate(self, session, seeded):
        case = await make_case(session, seeded)
        plan = next(t for t in case.tasks if t.name == "plan")
        plan.baseline_start = cases.now_utc() - timedelta(hours=3)
        await session.flush()

        await scans.deadline_scan(session)
        first = len(await notified(session, NotificationType.TASK_LATE_START))
        await scans.deadline_scan(session)
        # A scan running every five minutes must not become a firehose
        assert (
            len(await notified(session, NotificationType.TASK_LATE_START))
            == first
        )

    async def test_editing_the_task_reopens_the_alert_cycle(
        self, session, seeded
    ):
        case = await make_case(session, seeded)
        plan = next(t for t in case.tasks if t.name == "plan")
        plan.baseline_start = cases.now_utc() - timedelta(hours=3)
        await session.flush()

        await scans.deadline_scan(session)
        before = len(await notified(session, NotificationType.TASK_LATE_START))

        # The version acts as the alert epoch
        plan.version += 1
        await session.flush()
        await scans.deadline_scan(session)
        assert (
            len(await notified(session, NotificationType.TASK_LATE_START))
            > before
        )


class TestDedup:
    def test_key_includes_the_user(self):
        # Without the user id, notifying a second recipient of the same event
        # would be swallowed as a duplicate.
        first = notifications.dedup_key(
            1, NotificationType.TASK_OVERDUE, task_id=9
        )
        second = notifications.dedup_key(
            2, NotificationType.TASK_OVERDUE, task_id=9
        )
        assert first != second

    def test_case_scope_does_not_collide_across_cases(self):
        first = notifications.dedup_key(
            1, NotificationType.CASE_OVERDUE, case_id=1
        )
        second = notifications.dedup_key(
            1, NotificationType.CASE_OVERDUE, case_id=2
        )
        assert first != second

    def test_epoch_allows_a_fresh_cycle(self):
        first = notifications.dedup_key(
            1, NotificationType.TASK_OVERDUE, task_id=9, epoch=1
        )
        second = notifications.dedup_key(
            1, NotificationType.TASK_OVERDUE, task_id=9, epoch=2
        )
        assert first != second

    def test_undeduped_types_return_none(self):
        assert (
            notifications.dedup_key(
                1, NotificationType.TASK_ASSIGNED, task_id=9
            )
            is None
        )

    async def test_assignment_notifications_may_repeat(self, session, seeded):
        for _ in range(2):
            await notifications.notify(
                session,
                user_ids=[seeded["pm"].id],
                notification_type=NotificationType.TASK_ASSIGNED,
                title="You were assigned a task",
            )
        rows = await notified(session, NotificationType.TASK_ASSIGNED)
        assert len(rows) == 2


class TestDelivery:
    async def test_pending_deliveries_are_flushed(self, session, seeded):
        await notifications.notify(
            session,
            user_ids=[seeded["pm"].id],
            notification_type=NotificationType.TASK_ASSIGNED,
            title="Assigned",
        )
        sent = await notifications.flush_deliveries(session)
        assert sent >= 1

    async def test_in_app_survives_a_broken_channel(self, session, seeded):
        class Broken:
            name = "email"

            async def send(self, notification, recipient_email):
                raise RuntimeError("smtp is down")

        notifications.register_channel(Broken())
        try:
            created = await notifications.notify(
                session,
                user_ids=[seeded["pm"].id],
                notification_type=NotificationType.TASK_ASSIGNED,
                title="Assigned",
            )
            await notifications.flush_deliveries(session)
            # The in-app notification still exists; only delivery failed
            assert created
        finally:
            notifications.register_channel(notifications.LoggingEmailChannel())


class TestSweep:
    async def test_ready_api_tasks_get_queued(self, session, seeded):
        case = await make_case(session, seeded)
        by_name = {task.name: task for task in case.tasks}
        await cases.complete_task(session, seeded["pm"], case, by_name["plan"])

        queued = await scans.sweep_ready_tasks(session)
        assert queued == 1
        jobs = (
            await session.scalars(
                select(JobQueue).where(JobQueue.job_type == JobType.TRIGGER)
            )
        ).all()
        assert [job.case_task_id for job in jobs] == [by_name["test"].id]

    async def test_sweep_is_idempotent(self, session, seeded):
        case = await make_case(session, seeded)
        by_name = {task.name: task for task in case.tasks}
        await cases.complete_task(session, seeded["pm"], case, by_name["plan"])
        await scans.sweep_ready_tasks(session)
        # The sweep exists to catch missed work, so it must not double-fire
        assert await scans.sweep_ready_tasks(session) == 0

    async def test_manual_tasks_are_not_queued(self, session, seeded):
        case = await make_case(session, seeded)
        assert any(
            task.status is TaskStatus.READY and not task.task_api
            for task in case.tasks
        )
        assert await scans.sweep_ready_tasks(session) == 0


class TestForecastRefresh:
    async def test_active_cases_are_recalculated(self, session, seeded):
        case = await make_case(session, seeded)
        case.forecast_end = None
        await session.flush()

        assert await scans.refresh_forecasts(session) == 1
        assert case.forecast_end is not None
