"""Inserting, removing and reopening tasks (implement.md §8.4, §5.10).

Insertion is one of the original requirements -- "add a task between two
tasks" -- so the two wiring modes and the baseline consequence get explicit
coverage.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.models import TaskStatus
from app.services import cases, preview
from app.services.cases import CaseError, DeleteMode, InsertMode

TARGET = datetime(2026, 9, 30, 18, 0, tzinfo=UTC)


async def make_case(session, seeded):
    return await cases.create(
        session,
        seeded["admin"],
        name="Editable",
        template_name="launch",
        target_date=TARGET,
        params={"test_hours": 16, "needs_review": True},
        role_assignments={"owner": "pm", "tester": "qa"},
    )


async def edges_of(session, case) -> set[tuple[str, str]]:
    by_id = {task.id: task.name for task in case.tasks}
    return {
        (by_id[e.predecessor_id], by_id[e.successor_id])
        for e in await cases.dependencies(session, case.id)
        if e.predecessor_id in by_id and e.successor_id in by_id
    }


class TestInsertSerial:
    async def test_splices_into_the_chain(self, session, seeded):
        case = await make_case(session, seeded)
        before = await edges_of(session, case)
        assert ("plan", "test") in before

        await cases.insert_task(
            session,
            seeded["admin"],
            case,
            name="approval",
            display_name="Manager approval",
            duration_seconds=4 * 3600,
            predecessors=["plan"],
            successors=["test"],
            mode=InsertMode.SERIAL,
        )

        after = await edges_of(session, case)
        assert ("plan", "approval") in after
        assert ("approval", "test") in after
        # The link it was inserted into is cut, not left running alongside
        assert ("plan", "test") not in after

    async def test_inserted_task_has_no_baseline(self, session, seeded):
        case = await make_case(session, seeded)
        await cases.insert_task(
            session,
            seeded["admin"],
            case,
            name="approval",
            duration_seconds=3600,
            predecessors=["plan"],
            successors=["test"],
        )
        added = next(t for t in case.tasks if t.name == "approval")
        # There was no original plan for it, so a baseline would be fiction
        assert added.baseline_start is None
        assert added.is_unplanned
        # ...but it is forecast like everything else
        assert added.forecast_start is not None

    async def test_it_pushes_the_forecast_out(self, session, seeded):
        case = await make_case(session, seeded)
        before = case.forecast_end
        await cases.insert_task(
            session,
            seeded["admin"],
            case,
            name="approval",
            duration_seconds=48 * 3600,
            predecessors=["plan"],
            successors=["test"],
        )
        assert case.forecast_end > before

    async def test_it_inherits_its_neighbours_phase(self, session, seeded):
        case = await make_case(session, seeded)
        await cases.insert_task(
            session,
            seeded["admin"],
            case,
            name="approval",
            duration_seconds=3600,
            predecessors=["plan"],
            successors=["test"],
        )
        added = next(t for t in case.tasks if t.name == "approval")
        assert added.phase == "Prepare"


class TestInsertParallel:
    async def test_leaves_the_existing_link_alone(self, session, seeded):
        case = await make_case(session, seeded)
        await cases.insert_task(
            session,
            seeded["admin"],
            case,
            name="sidecar",
            duration_seconds=3600,
            predecessors=["plan"],
            mode=InsertMode.PARALLEL,
        )
        after = await edges_of(session, case)
        assert ("plan", "sidecar") in after
        # Hanging alongside must not disturb the chain it sits next to
        assert ("plan", "test") in after


class TestInsertGuards:
    async def test_duplicate_name_is_refused(self, session, seeded):
        case = await make_case(session, seeded)
        with pytest.raises(CaseError) as exc:
            await cases.insert_task(
                session, seeded["admin"], case, name="plan"
            )
        assert exc.value.code == "E_DUP_TASK_NAME"

    async def test_unknown_neighbour_is_refused(self, session, seeded):
        case = await make_case(session, seeded)
        with pytest.raises(CaseError) as exc:
            await cases.insert_task(
                session,
                seeded["admin"],
                case,
                name="x",
                predecessors=["ghost"],
            )
        assert exc.value.code == "E_UNKNOWN_REQUIREMENT"

    async def test_a_cycle_is_refused(self, session, seeded):
        case = await make_case(session, seeded)
        with pytest.raises(CaseError) as exc:
            await cases.insert_task(
                session,
                seeded["admin"],
                case,
                name="loop",
                predecessors=["report"],
                successors=["plan"],
                mode=InsertMode.PARALLEL,
            )
        assert exc.value.code == "E_CYCLE"

    async def test_cannot_insert_into_a_closed_case(self, session, seeded):
        case = await make_case(session, seeded)
        await cases.cancel(session, seeded["admin"], case)
        with pytest.raises(CaseError) as exc:
            await cases.insert_task(
                session, seeded["admin"], case, name="late"
            )
        assert exc.value.code == "E_NOT_ACTIVE"

    async def test_it_starts_blocked_behind_its_predecessor(
        self, session, seeded
    ):
        case = await make_case(session, seeded)
        await cases.insert_task(
            session,
            seeded["admin"],
            case,
            name="approval",
            duration_seconds=3600,
            predecessors=["plan"],
            successors=["test"],
        )
        added = next(t for t in case.tasks if t.name == "approval")
        assert added.status is TaskStatus.PENDING

        plan = next(t for t in case.tasks if t.name == "plan")
        await cases.complete_task(session, seeded["pm"], case, plan)
        assert added.status is TaskStatus.READY
        # ...and what it was spliced in front of stays blocked
        assert next(t for t in case.tasks if t.name == "test").status is (
            TaskStatus.PENDING
        )


class TestDelete:
    async def test_reconnect_stitches_neighbours(self, session, seeded):
        case = await make_case(session, seeded)
        test_task = next(t for t in case.tasks if t.name == "test")
        await cases.delete_task(
            session, seeded["admin"], case, test_task, DeleteMode.RECONNECT
        )
        after = await edges_of(session, case)
        assert "test" not in {t.name for t in case.tasks}
        # plan -> test -> report becomes plan -> report
        assert ("plan", "report") in after

    async def test_reconnect_sums_the_lag(self, session, seeded):
        case = await make_case(session, seeded)
        # plan -> test carries a 4H lag in the fixture template
        test_task = next(t for t in case.tasks if t.name == "test")
        await cases.delete_task(
            session, seeded["admin"], case, test_task, DeleteMode.RECONNECT
        )
        by_id = {task.id: task.name for task in case.tasks}
        stitched = next(
            e
            for e in await cases.dependencies(session, case.id)
            if by_id.get(e.predecessor_id) == "plan"
            and by_id.get(e.successor_id) == "report"
        )
        # The wait either side must not vanish with the step
        assert stitched.lag_seconds == 4 * 3600

    async def test_detach_frees_the_successors(self, session, seeded):
        case = await make_case(session, seeded)
        test_task = next(t for t in case.tasks if t.name == "test")
        await cases.delete_task(
            session, seeded["admin"], case, test_task, DeleteMode.DETACH
        )
        after = await edges_of(session, case)
        assert ("plan", "report") not in after

    async def test_started_work_cannot_be_deleted(self, session, seeded):
        case = await make_case(session, seeded)
        plan = next(t for t in case.tasks if t.name == "plan")
        await cases.complete_task(session, seeded["pm"], case, plan)
        with pytest.raises(CaseError) as exc:
            await cases.delete_task(session, seeded["admin"], case, plan)
        # Cancelling keeps the record; deleting would erase it
        assert exc.value.code == "E_TASK_NOT_DELETABLE"


class TestReopen:
    async def test_reopening_reblocks_downstream(self, session, seeded):
        case = await make_case(session, seeded)
        plan = next(t for t in case.tasks if t.name == "plan")
        test_task = next(t for t in case.tasks if t.name == "test")

        await cases.complete_task(session, seeded["pm"], case, plan)
        assert test_task.status is TaskStatus.READY

        await cases.reopen_task(session, seeded["admin"], case, plan)
        assert plan.status is TaskStatus.READY
        assert plan.actual_end is None
        assert test_task.status is TaskStatus.PENDING

    async def test_reopening_something_unfinished_is_refused(
        self, session, seeded
    ):
        case = await make_case(session, seeded)
        plan = next(t for t in case.tasks if t.name == "plan")
        with pytest.raises(CaseError) as exc:
            await cases.reopen_task(session, seeded["admin"], case, plan)
        assert exc.value.code == "E_NOT_DONE"


class TestResetBaseline:
    async def test_it_archives_what_it_replaces(self, session, seeded):
        case = await make_case(session, seeded)
        report = next(t for t in case.tasks if t.name == "report")
        original = report.baseline_start

        # Make the case actually slip; on a case still running to plan the
        # reset is legitimately a no-op.
        test_task = next(t for t in case.tasks if t.name == "test")
        await cases.update_task(
            session,
            seeded["admin"],
            case,
            test_task,
            duration_seconds=300 * 3600,
        )
        assert report.forecast_start != original

        await cases.reset_baseline(
            session, seeded["admin"], case, note="project restarted"
        )

        assert report.baseline_start == report.forecast_start
        assert report.baseline_start != original
        # Erasing what was promised without a record would defeat the point
        assert len(case.baseline_resets) == 1
        archived = case.baseline_resets[0]["baseline"]
        assert any(
            entry["name"] == "report"
            and entry["start"] == original.isoformat()
            for entry in archived
        )

    async def test_it_replaces_a_late_plan_with_an_early_one(
        self, session, seeded
    ):
        """Resetting changes the plan's character, not just its dates.

        The baseline is as-late-as-possible (backward pass from the target);
        the forecast is as-early-as-possible. So a reset moves every task with
        slack earlier, even on a case that is running exactly to plan. That is
        the documented behaviour, but it is not obvious, so it is pinned here.
        """
        case = await make_case(session, seeded)
        before = {t.name: t.baseline_start for t in case.tasks}

        await cases.reset_baseline(session, seeded["admin"], case)

        for task in case.tasks:
            assert task.baseline_start == task.forecast_start
        moved = [
            name
            for name, start in before.items()
            if next(t for t in case.tasks if t.name == name).baseline_start
            != start
        ]
        assert moved, "tasks with slack should have moved earlier"


class TestSimulate:
    async def test_a_longer_task_reports_the_knock_on(self, session, seeded):
        case = await make_case(session, seeded)
        result = await preview.simulate(
            session,
            case,
            task_name="test",
            duration_seconds=200 * 3600,
        )
        assert result.simulated_forecast_end > result.current_forecast_end
        assert result.delta_seconds > 0
        # It names what moves, not just that something did
        assert "report" in {item["name"] for item in result.affected}

    async def test_nothing_is_persisted(self, session, seeded):
        case = await make_case(session, seeded)
        before = case.forecast_end
        await preview.simulate(
            session, case, task_name="test", duration_seconds=500 * 3600
        )
        assert case.forecast_end == before
        assert (
            next(t for t in case.tasks if t.name == "test").duration_seconds
            == 16 * 3600
        )

    async def test_an_insertion_reports_its_cost(self, session, seeded):
        case = await make_case(session, seeded)
        result = await preview.simulate(
            session,
            case,
            insert_after="plan",
            insert_duration_seconds=72 * 3600,
        )
        assert result.delta_seconds > 0

    async def test_a_harmless_change_reports_no_movement(
        self, session, seeded
    ):
        case = await make_case(session, seeded)
        result = await preview.simulate(
            session, case, task_name="notify", duration_seconds=600
        )
        assert result.delta_seconds == 0
