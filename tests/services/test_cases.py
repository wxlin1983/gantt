"""Case lifecycle through the service layer (§5.7, §8.2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models import (
    CaseHealth,
    CaseStatus,
    CompletionSource,
    FailurePolicy,
    TaskStatus,
    TemplateStatus,
)
from app.services import cases
from app.services.cases import CaseError

TARGET = datetime(2026, 9, 30, 18, 0, tzinfo=UTC)


async def make_case(session, seeded, **overrides):
    kwargs = {
        "name": "Launch A",
        "template_name": "launch",
        "target_date": TARGET,
        "params": {"test_hours": 16, "needs_review": True},
        "role_assignments": {"owner": "pm", "tester": "qa"},
    }
    kwargs.update(overrides)
    return await cases.create(session, seeded["admin"], **kwargs)


class TestCreate:
    async def test_materialises_tasks_and_edges(self, session, seeded):
        case = await make_case(session, seeded)
        names = sorted(task.name for task in case.tasks)
        assert names == ["notify", "plan", "report", "review", "test"]

        edges = await cases.dependencies(session, case.id)
        by_id = {task.id: task.name for task in case.tasks}
        assert {
            (by_id[e.predecessor_id], by_id[e.successor_id], e.lag_seconds)
            for e in edges
        } == {
            ("plan", "test", 4 * 3600),
            ("plan", "review", 0),
            ("test", "report", 0),
            ("review", "report", 0),
            ("report", "notify", 0),
        }

    async def test_resolves_owners_from_roles(self, session, seeded):
        case = await make_case(session, seeded)
        by_name = {task.name: task for task in case.tasks}
        assert by_name["plan"].owner_id == seeded["pm"].id
        assert by_name["test"].owner_id == seeded["qa"].id
        # group_lead resolves against the group's lead member
        assert by_name["review"].owner_id == seeded["qa"].id
        # same_as copies from the referenced task
        assert by_name["report"].owner_id == seeded["pm"].id

    async def test_resolves_groups(self, session, seeded):
        case = await make_case(session, seeded)
        by_name = {task.name: task for task in case.tasks}
        assert by_name["plan"].group_id == seeded["rnd"].id
        assert by_name["test"].group_id == seeded["quality"].id

    async def test_parameter_drives_duration(self, session, seeded):
        case = await make_case(session, seeded, params={"test_hours": 40})
        by_name = {task.name: task for task in case.tasks}
        assert by_name["test"].duration_seconds == 40 * 3600

    async def test_conditional_task_is_skipped_and_recorded(
        self, session, seeded
    ):
        case = await make_case(
            session,
            seeded,
            params={"test_hours": 8, "needs_review": False},
        )
        assert "review" not in {task.name for task in case.tasks}
        assert [entry["id"] for entry in case.skipped_tasks] == ["review"]

        # report keeps its dependency on test, and picks up the bypass edge
        edges = await cases.dependencies(session, case.id)
        by_id = {task.id: task.name for task in case.tasks}
        pairs = {
            (by_id[e.predecessor_id], by_id[e.successor_id]) for e in edges
        }
        assert ("test", "report") in pairs
        assert ("plan", "report") in pairs

    async def test_baseline_is_computed_back_from_the_target(
        self, session, seeded
    ):
        case = await make_case(session, seeded)
        by_name = {task.name: task for task in case.tasks}
        # The plan ends a buffer's width before the target
        assert by_name["notify"].baseline_end == TARGET - timedelta(hours=8)
        assert all(task.baseline_start is not None for task in case.tasks)
        assert by_name["plan"].baseline_end <= by_name["test"].baseline_start

    async def test_buffer_is_stored(self, session, seeded):
        case = await make_case(session, seeded)
        assert case.buffer_seconds == 8 * 3600

    async def test_only_roots_start_ready(self, session, seeded):
        case = await make_case(session, seeded)
        by_name = {task.name: task for task in case.tasks}
        assert by_name["plan"].status is TaskStatus.READY
        assert by_name["test"].status is TaskStatus.PENDING
        assert by_name["report"].status is TaskStatus.PENDING

    async def test_forecast_and_health_are_populated(self, session, seeded):
        case = await make_case(session, seeded)
        assert case.forecast_end is not None
        assert case.health in set(CaseHealth)
        assert case.progress_ratio == 0.0
        assert any(task.is_on_critical_path for task in case.tasks)

    async def test_snapshot_is_self_contained(self, session, seeded):
        case = await make_case(session, seeded)
        snap = case.template_snapshot
        assert snap["snapshot_version"] == 1
        assert set(snap["task_templates"]) == {
            "tt_plan",
            "tt_test",
            "tt_report",
        }
        assert "taiwan_office" in snap["calendars"]

    async def test_idempotency_key_prevents_duplicates(self, session, seeded):
        first = await make_case(session, seeded, idempotency_key="abc")
        second = await make_case(session, seeded, idempotency_key="abc")
        assert first.id == second.id

    async def test_missing_required_role_is_refused(self, session, seeded):
        with pytest.raises(CaseError) as exc:
            await make_case(session, seeded, role_assignments={})
        assert exc.value.code == "E_MISSING_ROLE"

    async def test_role_assigned_to_a_nonexistent_user_is_refused(
        self, session, seeded
    ):
        """The DSL can only see that *a* name was given, not whose it is.

        Without this the case was created, owner resolution silently found
        nobody, and every task came out unassigned with nothing saying why.
        """
        with pytest.raises(CaseError) as exc:
            await make_case(
                session,
                seeded,
                role_assignments={"owner": "nobody", "tester": "qa"},
            )
        assert exc.value.code == "E_UNKNOWN_USER"
        # Names the one that is wrong, not just that something is
        assert "nobody" in str(exc.value)
        assert "qa" not in str(exc.value)

    async def test_a_valid_assignment_resolves_to_a_real_owner(
        self, session, seeded
    ):
        case = await make_case(session, seeded)
        owners = {task.name: task.owner_id for task in case.tasks}
        assert all(owner is not None for owner in owners.values()), owners

    async def test_parameter_out_of_range_is_refused(self, session, seeded):
        with pytest.raises(CaseError) as exc:
            await make_case(session, seeded, params={"test_hours": 500})
        assert exc.value.code == "E_BAD_PARAM_VALUE"

    async def test_unknown_template_is_refused(self, session, seeded):
        with pytest.raises(CaseError) as exc:
            await make_case(session, seeded, template_name="nope")
        assert exc.value.code == "E_TEMPLATE_NOT_FOUND"

    async def test_unpublished_template_is_not_usable(self, session, seeded):
        seeded["template"].status = TemplateStatus.DRAFT
        await session.flush()
        with pytest.raises(CaseError) as exc:
            await make_case(session, seeded)
        assert exc.value.code == "E_TEMPLATE_NOT_FOUND"


class TestCompletion:
    async def test_completing_a_root_promotes_its_successors(
        self, session, seeded
    ):
        case = await make_case(session, seeded)
        by_name = {task.name: task for task in case.tasks}

        await cases.complete_task(
            session, seeded["pm"], case, by_name["plan"], note="done early"
        )

        assert by_name["plan"].status is TaskStatus.DONE
        assert by_name["plan"].completion_source is CompletionSource.MANUAL
        assert by_name["plan"].completed_by_id == seeded["pm"].id
        assert by_name["test"].status is TaskStatus.READY
        assert by_name["review"].status is TaskStatus.READY
        # report still waits for both of its predecessors
        assert by_name["report"].status is TaskStatus.PENDING

    async def test_progress_is_duration_weighted(self, session, seeded):
        case = await make_case(session, seeded)
        by_name = {task.name: task for task in case.tasks}
        total = sum(task.duration_seconds for task in case.tasks)
        await cases.complete_task(session, seeded["pm"], case, by_name["plan"])
        expected = by_name["plan"].duration_seconds / total
        assert case.progress_ratio == pytest.approx(expected, abs=1e-4)

    async def test_forecast_moves_when_a_task_finishes_early(
        self, session, seeded
    ):
        case = await make_case(session, seeded)
        before = case.forecast_end
        by_name = {task.name: task for task in case.tasks}
        await cases.complete_task(session, seeded["pm"], case, by_name["plan"])
        assert case.forecast_end != before

    async def test_double_completion_is_refused(self, session, seeded):
        case = await make_case(session, seeded)
        by_name = {task.name: task for task in case.tasks}
        await cases.complete_task(session, seeded["pm"], case, by_name["plan"])
        with pytest.raises(CaseError) as exc:
            await cases.complete_task(
                session, seeded["pm"], case, by_name["plan"]
            )
        assert exc.value.code == "E_ALREADY_DONE"

    async def test_future_completion_is_refused(self, session, seeded):
        case = await make_case(session, seeded)
        by_name = {task.name: task for task in case.tasks}
        with pytest.raises(CaseError) as exc:
            await cases.complete_task(
                session,
                seeded["pm"],
                case,
                by_name["plan"],
                at=datetime.now(tz=UTC) + timedelta(hours=1),
            )
        assert exc.value.code == "E_FUTURE_COMPLETION"

    async def test_backfilled_completion_is_allowed(self, session, seeded):
        # Reporting after the fact is the norm, not an exception.
        case = await make_case(session, seeded)
        by_name = {task.name: task for task in case.tasks}
        earlier = datetime.now(tz=UTC) - timedelta(hours=3)
        await cases.complete_task(
            session, seeded["pm"], case, by_name["plan"], at=earlier
        )
        assert by_name["plan"].actual_end == earlier

    async def test_case_completes_and_cancels_leftover_optionals(
        self, session, seeded
    ):
        case = await make_case(session, seeded)
        by_name = {task.name: task for task in case.tasks}
        for name in ("plan", "test", "review", "report"):
            await cases.complete_task(
                session, seeded["admin"], case, by_name[name]
            )

        assert case.status is CaseStatus.COMPLETED
        assert case.completed_at is not None
        # notify is optional and unfinished, so it is closed out explicitly
        assert by_name["notify"].status is TaskStatus.CANCELLED

    async def test_audit_trail_records_the_actor(self, session, seeded):
        from sqlalchemy import select

        from app.models import AuditEvent

        case = await make_case(session, seeded)
        by_name = {task.name: task for task in case.tasks}
        await cases.complete_task(
            session, seeded["qa"], case, by_name["plan"], note="covering"
        )
        events = (
            await session.scalars(
                select(AuditEvent).where(AuditEvent.case_id == case.id)
            )
        ).all()
        kinds = {event.event_type for event in events}
        assert {"case.created", "task.completed"} <= kinds
        completion = next(
            e for e in events if e.event_type == "task.completed"
        )
        # The real operator is recorded even when covering for the owner
        assert completion.actor_id == seeded["qa"].id
        assert completion.note == "covering"


class TestSettlementRules:
    async def test_cancelled_predecessor_releases_successors(
        self, session, seeded
    ):
        case = await make_case(session, seeded)
        by_name = {task.name: task for task in case.tasks}
        by_name["plan"].status = TaskStatus.CANCELLED
        await session.flush()

        promoted = cases.promote_ready(
            case.tasks, await cases.dependencies(session, case.id)
        )
        assert by_name["test"] in promoted
        assert by_name["test"].status is TaskStatus.READY

    async def test_blocking_failure_holds_successors(self, session, seeded):
        case = await make_case(session, seeded)
        by_name = {task.name: task for task in case.tasks}
        by_name["plan"].status = TaskStatus.FAILED
        by_name["plan"].on_failure = FailurePolicy.BLOCK
        await session.flush()

        cases.promote_ready(
            case.tasks, await cases.dependencies(session, case.id)
        )
        assert by_name["test"].status is TaskStatus.PENDING

    async def test_continue_policy_releases_successors(self, session, seeded):
        case = await make_case(session, seeded)
        by_name = {task.name: task for task in case.tasks}
        by_name["plan"].status = TaskStatus.FAILED
        by_name["plan"].on_failure = FailurePolicy.CONTINUE
        await session.flush()

        cases.promote_ready(
            case.tasks, await cases.dependencies(session, case.id)
        )
        assert by_name["test"].status is TaskStatus.READY

    async def test_reopening_a_task_reblocks_downstream(self, session, seeded):
        case = await make_case(session, seeded)
        by_name = {task.name: task for task in case.tasks}
        await cases.complete_task(session, seeded["pm"], case, by_name["plan"])
        assert by_name["test"].status is TaskStatus.READY

        by_name["plan"].status = TaskStatus.RUNNING
        await session.flush()
        cases.promote_ready(
            case.tasks, await cases.dependencies(session, case.id)
        )
        assert by_name["test"].status is TaskStatus.PENDING


class TestUpdateTask:
    async def test_duration_change_reforecasts(self, session, seeded):
        case = await make_case(session, seeded)
        by_name = {task.name: task for task in case.tasks}
        before = case.forecast_end

        await cases.update_task(
            session,
            seeded["pm"],
            case,
            by_name["test"],
            duration_seconds=80 * 3600,
        )
        assert by_name["test"].duration_seconds == 80 * 3600
        assert case.forecast_end > before

    async def test_baseline_is_untouched_by_edits(self, session, seeded):
        case = await make_case(session, seeded)
        by_name = {task.name: task for task in case.tasks}
        original = by_name["test"].baseline_start

        await cases.update_task(
            session,
            seeded["pm"],
            case,
            by_name["test"],
            duration_seconds=80 * 3600,
        )
        assert by_name["test"].baseline_start == original

    async def test_owner_change_marks_the_source_manual(self, session, seeded):
        case = await make_case(session, seeded)
        by_name = {task.name: task for task in case.tasks}
        assert by_name["test"].owner_source == "role:tester"

        await cases.update_task(
            session,
            seeded["pm"],
            case,
            by_name["test"],
            owner_id=seeded["outsider"].id,
        )
        # A later bulk role reassignment must not undo this
        assert by_name["test"].owner_source == "manual"

    async def test_a_task_can_be_taken_off_its_owner(self, session, seeded):
        """`None` used to mean "leave alone", so this was impossible."""
        case = await make_case(session, seeded)
        by_name = {task.name: task for task in case.tasks}
        assert by_name["test"].owner_id is not None

        await cases.update_task(
            session, seeded["pm"], case, by_name["test"], owner_id=None
        )
        assert by_name["test"].owner_id is None
        # Deliberately nobody, which is not the same as a role that never
        # resolved -- the UI says so differently.
        assert by_name["test"].owner_source == "manual"

    async def test_omitting_the_owner_leaves_it_alone(self, session, seeded):
        case = await make_case(session, seeded)
        by_name = {task.name: task for task in case.tasks}
        before = by_name["test"].owner_id

        await cases.update_task(
            session, seeded["pm"], case, by_name["test"], duration_seconds=3600
        )
        assert by_name["test"].owner_id == before
        assert by_name["test"].owner_source == "role:tester"

    async def test_stale_version_is_rejected(self, session, seeded):
        case = await make_case(session, seeded)
        by_name = {task.name: task for task in case.tasks}
        with pytest.raises(CaseError) as exc:
            await cases.update_task(
                session,
                seeded["pm"],
                case,
                by_name["test"],
                duration_seconds=3600,
                expected_version=999,
            )
        assert exc.value.code == "E_STALE_WRITE"

    async def test_negative_duration_is_rejected(self, session, seeded):
        case = await make_case(session, seeded)
        by_name = {task.name: task for task in case.tasks}
        with pytest.raises(CaseError) as exc:
            await cases.update_task(
                session,
                seeded["pm"],
                case,
                by_name["test"],
                duration_seconds=-1,
            )
        assert exc.value.code == "E_BAD_DURATION"


class TestTargetDate:
    async def test_baseline_survives_a_target_change(self, session, seeded):
        case = await make_case(session, seeded)
        by_name = {task.name: task for task in case.tasks}
        original = by_name["plan"].baseline_start

        await cases.set_target_date(
            session,
            seeded["admin"],
            case,
            TARGET + timedelta(days=7),
            note="customer moved it",
        )
        # The baseline is the record of what was promised; it must not move
        assert by_name["plan"].baseline_start == original
        assert case.target_date == TARGET + timedelta(days=7)

    async def test_change_is_recorded_in_history(self, session, seeded):
        case = await make_case(session, seeded)
        await cases.set_target_date(
            session, seeded["admin"], case, TARGET + timedelta(days=7)
        )
        assert len(case.target_date_history) == 1
        entry = case.target_date_history[0]
        assert entry["from"] == TARGET.isoformat()
        assert entry["by"] == seeded["admin"].id


class TestCancel:
    async def test_cancels_unsettled_tasks(self, session, seeded):
        case = await make_case(session, seeded)
        by_name = {task.name: task for task in case.tasks}
        await cases.complete_task(session, seeded["pm"], case, by_name["plan"])

        await cases.cancel(session, seeded["admin"], case, note="descoped")
        assert case.status is CaseStatus.CANCELLED
        assert by_name["plan"].status is TaskStatus.DONE
        assert by_name["test"].status is TaskStatus.CANCELLED

    async def test_cancelling_twice_is_refused(self, session, seeded):
        case = await make_case(session, seeded)
        await cases.cancel(session, seeded["admin"], case)
        with pytest.raises(CaseError) as exc:
            await cases.cancel(session, seeded["admin"], case)
        assert exc.value.code == "E_NOT_ACTIVE"
