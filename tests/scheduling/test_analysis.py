"""Critical path, weighted progress and buffer health (§5.5, §5.8, §5.9)."""

from __future__ import annotations

import pytest

from app.models.enums import CaseHealth, FailurePolicy, TaskStatus
from app.scheduling import (
    ScheduleEdge,
    backward_pass,
    critical_path,
    evaluate,
    forward_pass,
    late_starts,
    progress_ratio,
)

from .conftest import at, chain, task

TARGET = "2026-08-15 18:00"


def with_baseline(tasks, edges, target=TARGET, buffer_seconds=0):
    baseline = backward_pass(
        tasks, edges, at(target), buffer_seconds=buffer_seconds
    )
    for entry in tasks:
        entry.baseline_start, entry.baseline_end = baseline[entry.id]
    return baseline


class TestCriticalPath:
    def test_linear_chain_is_entirely_critical(self):
        tasks = [task(f"t{i}", 12) for i in (1, 2, 3)]
        edges = chain("t1", "t2", "t3")
        with_baseline(tasks, edges)
        now = at("2026-08-14 06:00")
        forecast = forward_pass(tasks, edges, now)
        assert critical_path(tasks, edges, forecast, now) == {"t1", "t2", "t3"}

    def test_only_the_longer_branch_is_critical(self):
        tasks = [task("a", 2), task("slow", 10), task("fast", 2), task("z", 2)]
        edges = [
            ScheduleEdge("a", "slow"),
            ScheduleEdge("a", "fast"),
            ScheduleEdge("slow", "z"),
            ScheduleEdge("fast", "z"),
        ]
        with_baseline(tasks, edges)
        now = at("2026-08-15 04:00")
        forecast = forward_pass(tasks, edges, now)
        marked = critical_path(tasks, edges, forecast, now)
        assert "slow" in marked
        assert "fast" not in marked
        assert {"a", "z"} <= marked

    def test_finished_tasks_are_not_marked(self):
        tasks = [task("a", 4), task("b", 4)]
        edges = [ScheduleEdge("a", "b")]
        with_baseline(tasks, edges)
        tasks[0].status = TaskStatus.DONE
        tasks[0].actual_start = at("2026-08-15 06:00")
        tasks[0].actual_end = at("2026-08-15 10:00")
        now = at("2026-08-15 10:00")
        forecast = forward_pass(tasks, edges, now)
        marked = critical_path(tasks, edges, forecast, now)
        assert "a" not in marked

    def test_optional_tasks_are_not_marked(self):
        tasks = [task("main", 12), task("extra", 12, is_optional=True)]
        edges = [ScheduleEdge("main", "extra")]
        with_baseline(tasks, edges)
        now = at("2026-08-14 06:00")
        forecast = forward_pass(tasks, edges, now)
        marked = critical_path(tasks, edges, forecast, now)
        assert "extra" not in marked

    def test_never_empty_while_work_remains(self):
        """The highlight design.md §4.3 promises must always exist.

        Float is measured against the case's own forecast finish, not the
        target date -- otherwise a case running comfortably early would show no
        critical path at all.
        """
        tasks = [task("a", 2), task("b", 3)]
        edges = [ScheduleEdge("a", "b")]
        now = at("2026-08-10 06:00")  # eleven days of slack
        forecast = forward_pass(tasks, edges, now)
        assert critical_path(tasks, edges, forecast, now) == {"a", "b"}

    def test_empty_once_everything_is_settled(self):
        tasks = [task("a", 2, status=TaskStatus.DONE)]
        tasks[0].actual_start = at("2026-08-14 08:00")
        tasks[0].actual_end = at("2026-08-14 10:00")
        now = at("2026-08-14 10:00")
        forecast = forward_pass(tasks, [], now)
        assert critical_path(tasks, [], forecast, now) == set()

    def test_optional_delay_still_propagates(self):
        """Not marking an optional task does not free its successors.

        The flag changes the highlight, not the graph.
        """
        tasks = [task("opt", 12, is_optional=True), task("after", 4)]
        edges = [ScheduleEdge("opt", "after")]
        now = at("2026-08-14 06:00")
        forecast = forward_pass(tasks, edges, now)
        assert forecast.start_of("after") == forecast.end_of("opt")


class TestWeightedProgress:
    def test_weighted_by_duration_not_count(self):
        # One long task done out of one long and three tiny ones is most of
        # the work, even though it is only a quarter of the task count.
        tasks = [
            task("big", 12, status=TaskStatus.DONE),
            task("tiny1", 0.1),
            task("tiny2", 0.1),
            task("tiny3", 0.1),
        ]
        ratio = progress_ratio(tasks, at("2026-08-14 12:00"))
        assert ratio == pytest.approx(12 / 12.3, abs=1e-4)

    def test_running_task_contributes_time_spent(self):
        tasks = [task("a", 10, status=TaskStatus.RUNNING)]
        tasks[0].actual_start = at("2026-08-14 08:00")
        ratio = progress_ratio(tasks, at("2026-08-14 12:00"))
        assert ratio == pytest.approx(0.4)

    def test_running_task_cannot_exceed_its_own_weight(self):
        tasks = [task("a", 4, status=TaskStatus.RUNNING)]
        tasks[0].actual_start = at("2026-08-14 08:00")
        ratio = progress_ratio(tasks, at("2026-08-16 08:00"))
        assert ratio == 1.0

    def test_cancelled_counts_as_settled(self):
        tasks = [
            task("a", 5, status=TaskStatus.CANCELLED),
            task("b", 5),
        ]
        assert progress_ratio(tasks, at("2026-08-14 12:00")) == 0.5

    def test_failed_with_continue_counts_as_settled(self):
        tasks = [
            task(
                "a",
                5,
                status=TaskStatus.FAILED,
                on_failure=FailurePolicy.CONTINUE,
            ),
            task("b", 5),
        ]
        assert progress_ratio(tasks, at("2026-08-14 12:00")) == 0.5

    def test_failed_and_blocking_does_not_count(self):
        tasks = [
            task(
                "a",
                5,
                status=TaskStatus.FAILED,
                on_failure=FailurePolicy.BLOCK,
            ),
            task("b", 5),
        ]
        assert progress_ratio(tasks, at("2026-08-14 12:00")) == 0.0

    def test_all_zero_duration_tasks(self):
        tasks = [task("a", 0), task("b", 0)]
        assert progress_ratio(tasks, at("2026-08-14 12:00")) == 1.0


class TestLateStarts:
    def test_ready_task_past_its_baseline_start(self):
        tasks = [task("t", 4, status=TaskStatus.READY)]
        tasks[0].baseline_start = at("2026-08-14 08:00")
        assert late_starts(tasks, at("2026-08-14 12:00")) == ["t"]

    def test_ready_task_before_its_baseline_start(self):
        tasks = [task("t", 4, status=TaskStatus.READY)]
        tasks[0].baseline_start = at("2026-08-14 16:00")
        assert late_starts(tasks, at("2026-08-14 12:00")) == []

    def test_pending_task_is_not_late(self):
        # Not its turn yet; the blame belongs upstream.
        tasks = [task("t", 4, status=TaskStatus.PENDING)]
        tasks[0].baseline_start = at("2026-08-14 08:00")
        assert late_starts(tasks, at("2026-08-14 12:00")) == []

    def test_unplanned_task_is_never_late(self):
        tasks = [task("t", 4, status=TaskStatus.READY)]
        assert tasks[0].baseline_start is None
        assert late_starts(tasks, at("2026-08-14 12:00")) == []


class TestHealthWithoutBuffer:
    def test_on_track(self):
        tasks = [task("t", 12)]
        with_baseline(tasks, [])
        outlook = evaluate(tasks, [], at(TARGET), at("2026-08-15 06:00"))
        assert outlook.health is CaseHealth.ON_TRACK
        assert outlook.exceeds_target_by_seconds == 0

    def test_overdue_when_the_forecast_passes_the_target(self):
        tasks = [task("t", 12)]
        outlook = evaluate(tasks, [], at(TARGET), at("2026-08-15 12:00"))
        assert outlook.health is CaseHealth.OVERDUE
        assert outlook.exceeds_target_by_seconds == 6 * 3600

    def test_at_risk_on_a_late_start(self):
        # Slack remains, so the forecast still lands inside the target -- but
        # a task that should already have begun is worth a warning.
        tasks = [task("t", 2, status=TaskStatus.READY)]
        tasks[0].baseline_start = at("2026-08-14 08:00")
        tasks[0].baseline_end = at("2026-08-14 10:00")
        outlook = evaluate(tasks, [], at(TARGET), at("2026-08-15 06:00"))
        assert outlook.late_starts == ["t"]
        assert outlook.exceeds_target_by_seconds == 0
        assert outlook.health is CaseHealth.AT_RISK

    def test_at_risk_on_a_blocking_failure(self):
        tasks = [
            task(
                "t",
                1,
                status=TaskStatus.FAILED,
                on_failure=FailurePolicy.BLOCK,
            )
        ]
        outlook = evaluate(tasks, [], at(TARGET), at("2026-08-15 10:00"))
        assert outlook.blocking_failures == ["t"]
        assert outlook.health is CaseHealth.AT_RISK


class TestHealthWithBuffer:
    """Buffer consumption answers "should I worry now" (§5.8)."""

    BUFFER = 10 * 3600

    def build(self, done_hours: float, total_hours: float = 10):
        """One finished task and one pending, to control the progress ratio."""
        tasks = [
            task("done", done_hours, status=TaskStatus.DONE),
            task("rest", total_hours - done_hours),
        ]
        tasks[0].actual_start = at("2026-08-14 00:00")
        tasks[0].actual_end = at("2026-08-14 00:00")
        return tasks

    def test_on_track_when_burn_trails_progress(self):
        tasks = self.build(done_hours=8)
        outlook = evaluate(
            tasks,
            [],
            at(TARGET),
            at("2026-08-15 06:00"),
            buffer_seconds=self.BUFFER,
        )
        assert outlook.progress_ratio == pytest.approx(0.8)
        assert outlook.buffer_consumed_ratio < outlook.progress_ratio
        assert outlook.health is CaseHealth.ON_TRACK

    def test_at_risk_when_burn_outpaces_progress(self):
        # 30% of the work done but 90% of the buffer already committed. The
        # forecast still lands before the target, which is exactly the case a
        # plain deadline comparison would call healthy.
        tasks = self.build(done_hours=3)
        outlook = evaluate(
            tasks,
            [],
            at(TARGET),
            at("2026-08-15 10:00"),
            buffer_seconds=self.BUFFER,
        )
        assert outlook.progress_ratio == pytest.approx(0.3)
        assert outlook.buffer_consumed_ratio == pytest.approx(0.9)
        assert outlook.exceeds_target_by_seconds == 0
        assert outlook.health is CaseHealth.AT_RISK

    def test_overdue_when_the_buffer_is_spent(self):
        tasks = self.build(done_hours=3)
        outlook = evaluate(
            tasks,
            [],
            at(TARGET),
            at("2026-08-15 18:00"),
            buffer_seconds=self.BUFFER,
        )
        assert outlook.buffer_consumed_ratio > 1.0
        assert outlook.health is CaseHealth.OVERDUE

    def test_buffer_moves_the_plan_deadline_earlier(self):
        tasks = [task("t", 4)]
        outlook = evaluate(
            tasks,
            [],
            at(TARGET),
            at("2026-08-15 06:00"),
            buffer_seconds=self.BUFFER,
        )
        assert outlook.plan_deadline == at("2026-08-15 08:00")
        assert outlook.target_date == at(TARGET)

    def test_zero_buffer_reports_no_consumption(self):
        tasks = [task("t", 4)]
        outlook = evaluate(tasks, [], at(TARGET), at("2026-08-15 06:00"))
        assert outlook.buffer_consumed_ratio == 0.0
