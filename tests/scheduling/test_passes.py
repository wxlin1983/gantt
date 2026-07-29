"""Backward and forward passes (implement.md §5.2, §5.3, §5.6)."""

from __future__ import annotations

import pytest

from app.dsl.graph import CycleError
from app.models.enums import FailurePolicy, TaskStatus
from app.scheduling import (
    ScheduleEdge,
    backward_pass,
    forward_pass,
    registry,
)

from .conftest import at, chain, hhmm, task


class TestWorkedExample:
    """The hand-computable example from implement.md §5.6.

    Three twelve-hour tasks in a line, continuous time, target
    2026-08-15 18:00. If this drifts, the document and the engine disagree.
    """

    TARGET = "2026-08-15 18:00"

    @pytest.fixture
    def tasks(self):
        return [task(f"my_task{i}") for i in (1, 2, 3)]

    @pytest.fixture
    def edges(self):
        return chain("my_task1", "my_task2", "my_task3")

    def test_backward_pass(self, tasks, edges):
        result = backward_pass(tasks, edges, at(self.TARGET))
        assert {
            key: (hhmm(value.start), hhmm(value.end))
            for key, value in result.intervals.items()
        } == {
            "my_task1": ("2026-08-14 06:00", "2026-08-14 18:00"),
            "my_task2": ("2026-08-14 18:00", "2026-08-15 06:00"),
            "my_task3": ("2026-08-15 06:00", "2026-08-15 18:00"),
        }

    def test_critical_path_length_is_36_hours(self, tasks, edges):
        result = backward_pass(tasks, edges, at(self.TARGET))
        span = at(self.TARGET) - result.earliest_start
        assert span.total_seconds() == 36 * 3600
        assert hhmm(result.earliest_start) == "2026-08-14 06:00"

    def test_forward_pass_with_progress(self, tasks, edges):
        """§5.6's second table: task1 finished early, task2 still running."""
        baseline = backward_pass(tasks, edges, at(self.TARGET))
        for entry in tasks:
            entry.baseline_start, entry.baseline_end = baseline[entry.id]

        tasks[0].status = TaskStatus.DONE
        tasks[0].actual_start = at("2026-08-14 06:00")
        tasks[0].actual_end = at("2026-08-14 08:00")
        tasks[1].status = TaskStatus.RUNNING
        tasks[1].actual_start = at("2026-08-14 08:30")

        result = forward_pass(tasks, edges, at("2026-08-14 20:30"))
        assert {
            key: (hhmm(value.start), hhmm(value.end))
            for key, value in result.intervals.items()
        } == {
            "my_task1": ("2026-08-14 06:00", "2026-08-14 08:00"),
            "my_task2": ("2026-08-14 08:30", "2026-08-14 20:30"),
            "my_task3": ("2026-08-14 20:30", "2026-08-15 08:30"),
        }
        assert hhmm(result.latest_end) == "2026-08-15 08:30"

    def test_forward_pass_when_a_task_overruns(self, tasks, edges):
        """§5.6's closing note: task2 slipping to 14:00 pushes past target."""
        tasks[0].status = TaskStatus.DONE
        tasks[0].actual_start = at("2026-08-14 06:00")
        tasks[0].actual_end = at("2026-08-14 08:00")
        tasks[1].status = TaskStatus.DONE
        tasks[1].actual_start = at("2026-08-14 08:30")
        tasks[1].actual_end = at("2026-08-15 14:00")

        result = forward_pass(tasks, edges, at("2026-08-15 14:00"))
        assert hhmm(result.end_of("my_task3")) == "2026-08-16 02:00"
        overshoot = result.latest_end - at(self.TARGET)
        assert overshoot.total_seconds() == 8 * 3600


class TestBackwardPassShapes:
    def test_multiple_sinks_each_align_to_the_target(self):
        tasks = [task("a", 2), task("b", 4)]
        result = backward_pass(tasks, [], at("2026-08-15 18:00"))
        assert hhmm(result.end_of("a")) == "2026-08-15 18:00"
        assert hhmm(result.end_of("b")) == "2026-08-15 18:00"

    def test_diamond_is_constrained_by_the_longer_branch(self):
        tasks = [task("a", 2), task("left", 2), task("right", 8), task("z", 2)]
        edges = [
            ScheduleEdge("a", "left"),
            ScheduleEdge("a", "right"),
            ScheduleEdge("left", "z"),
            ScheduleEdge("right", "z"),
        ]
        result = backward_pass(tasks, edges, at("2026-08-15 18:00"))
        assert hhmm(result.end_of("z")) == "2026-08-15 18:00"
        assert hhmm(result.start_of("z")) == "2026-08-15 16:00"
        # Both branches must finish by z's start
        assert hhmm(result.end_of("left")) == "2026-08-15 16:00"
        assert hhmm(result.end_of("right")) == "2026-08-15 16:00"
        # a is pushed back by the longer branch, not the shorter one
        assert hhmm(result.end_of("a")) == "2026-08-15 08:00"

    def test_zero_duration_task(self):
        result = backward_pass([task("m", 0)], [], at("2026-08-15 18:00"))
        assert result.start_of("m") == result.end_of("m")

    def test_single_task(self):
        result = backward_pass([task("only", 5)], [], at("2026-08-15 18:00"))
        assert hhmm(result.start_of("only")) == "2026-08-15 13:00"

    def test_cycle_is_reported(self):
        tasks = [task("a", 1), task("b", 1)]
        edges = [ScheduleEdge("a", "b"), ScheduleEdge("b", "a")]
        with pytest.raises(CycleError):
            backward_pass(tasks, edges, at("2026-08-15 18:00"))


class TestLag:
    def test_backward_pass_rewinds_the_lag(self):
        tasks = [task("a", 2), task("b", 2)]
        edges = [ScheduleEdge("a", "b", 4 * 3600)]
        result = backward_pass(tasks, edges, at("2026-08-15 18:00"))
        assert hhmm(result.start_of("b")) == "2026-08-15 16:00"
        # a must finish four hours before b starts
        assert hhmm(result.end_of("a")) == "2026-08-15 12:00"

    def test_forward_pass_adds_the_lag(self):
        tasks = [task("a", 2), task("b", 2)]
        edges = [ScheduleEdge("a", "b", 4 * 3600)]
        tasks[0].status = TaskStatus.DONE
        tasks[0].actual_start = at("2026-08-14 08:00")
        tasks[0].actual_end = at("2026-08-14 10:00")
        result = forward_pass(tasks, edges, at("2026-08-14 10:00"))
        assert hhmm(result.start_of("b")) == "2026-08-14 14:00"

    def test_lag_uses_the_successor_calendar(self, calendars):
        """ "Hand over four hours later" means four hours of the next
        person's working time, so a Friday evening finish waits for Monday."""
        tasks = [
            task("auto", 1),
            task("review", 1, calendar="taiwan_office"),
        ]
        edges = [ScheduleEdge("auto", "review", 4 * 3600)]
        tasks[0].status = TaskStatus.DONE
        tasks[0].actual_start = at("2026-08-14 16:00")
        tasks[0].actual_end = at("2026-08-14 17:00")
        result = forward_pass(tasks, edges, at("2026-08-14 17:00"), calendars)
        # 1h remains on Friday, the other 3h of lag land on Monday morning
        assert hhmm(result.start_of("review")) == "2026-08-17 12:00"


class TestMixedCalendars:
    def test_business_task_skips_the_weekend(self, calendars):
        tasks = [task("review", 12, calendar="taiwan_office")]
        result = backward_pass(
            tasks, [], at("2026-08-17 18:00"), calendars=calendars
        )
        # 12 working hours back from Monday 18:00: 9h Monday, 3h Friday
        assert hhmm(result.start_of("review")) == "2026-08-14 15:00"

    def test_continuous_and_business_in_one_case(self, calendars):
        tasks = [
            task("build", 6),
            task("approve", 9, calendar="taiwan_office"),
        ]
        edges = [ScheduleEdge("build", "approve")]
        result = backward_pass(
            tasks, edges, at("2026-08-17 18:00"), calendars=calendars
        )
        assert hhmm(result.start_of("approve")) == "2026-08-17 09:00"
        # build runs 24x7, so it simply ends when approve starts
        assert hhmm(result.end_of("build")) == "2026-08-17 09:00"
        assert hhmm(result.start_of("build")) == "2026-08-17 03:00"

    def test_day_units_mean_working_days_on_a_business_calendar(
        self, calendars
    ):
        from app.scheduling import ScheduleTask

        # 2D is two nine-hour days, not 172800 working seconds
        tasks = [
            ScheduleTask(
                id="audit",
                duration_seconds=2 * 86400,
                duration_days=2,
                calendar="taiwan_office",
            )
        ]
        result = backward_pass(
            tasks, [], at("2026-08-18 18:00"), calendars=calendars
        )
        assert hhmm(result.start_of("audit")) == "2026-08-17 09:00"

    def test_day_units_are_wall_clock_on_continuous_time(self):
        from app.scheduling import ScheduleTask

        tasks = [
            ScheduleTask(
                id="soak", duration_seconds=2 * 86400, duration_days=2
            )
        ]
        result = backward_pass(tasks, [], at("2026-08-18 18:00"))
        assert hhmm(result.start_of("soak")) == "2026-08-16 18:00"


class TestBuffer:
    def test_buffer_shifts_the_whole_plan_earlier(self):
        tasks = [task("a", 12)]
        without = backward_pass(tasks, [], at("2026-08-15 18:00"))
        with_buffer = backward_pass(
            tasks, [], at("2026-08-15 18:00"), buffer_seconds=8 * 3600
        )
        assert hhmm(without.end_of("a")) == "2026-08-15 18:00"
        assert hhmm(with_buffer.end_of("a")) == "2026-08-15 10:00"
        delta = without.end_of("a") - with_buffer.end_of("a")
        assert delta.total_seconds() == 8 * 3600


class TestForwardPassStates:
    def test_running_task_is_credited_with_time_spent(self):
        tasks = [task("t", 12, status=TaskStatus.RUNNING)]
        tasks[0].actual_start = at("2026-08-14 08:00")
        # Six hours in, six to go
        result = forward_pass(tasks, [], at("2026-08-14 14:00"))
        assert hhmm(result.start_of("t")) == "2026-08-14 08:00"
        assert hhmm(result.end_of("t")) == "2026-08-14 20:00"

    def test_running_task_past_its_duration_finishes_now(self):
        tasks = [task("t", 12, status=TaskStatus.RUNNING)]
        tasks[0].actual_start = at("2026-08-14 08:00")
        result = forward_pass(tasks, [], at("2026-08-15 08:00"))
        assert hhmm(result.end_of("t")) == "2026-08-15 08:00"

    def test_running_business_task_does_not_gain_weekend_progress(
        self, calendars
    ):
        tasks = [
            task("t", 9, status=TaskStatus.RUNNING, calendar="taiwan_office")
        ]
        tasks[0].actual_start = at("2026-08-14 17:00")
        # One working hour spent on Friday; eight remain from Monday
        result = forward_pass(tasks, [], at("2026-08-17 09:00"), calendars)
        assert hhmm(result.end_of("t")) == "2026-08-17 17:00"

    def test_pending_task_never_starts_in_the_past(self):
        tasks = [task("t", 4)]
        tasks[0].baseline_start = at("2026-08-10 09:00")
        result = forward_pass(tasks, [], at("2026-08-14 12:00"))
        assert hhmm(result.start_of("t")) == "2026-08-14 12:00"

    def test_pending_task_respects_a_future_baseline(self):
        tasks = [task("t", 4)]
        tasks[0].baseline_start = at("2026-08-20 09:00")
        result = forward_pass(tasks, [], at("2026-08-14 12:00"))
        assert hhmm(result.start_of("t")) == "2026-08-20 09:00"

    def test_unplanned_task_without_a_baseline_starts_now(self):
        """A task inserted mid-flight has no baseline (§5.10)."""
        tasks = [task("added", 4)]
        assert tasks[0].baseline_start is None
        result = forward_pass(tasks, [], at("2026-08-14 12:00"))
        assert hhmm(result.start_of("added")) == "2026-08-14 12:00"

    def test_cancelled_predecessor_releases_its_successor(self):
        tasks = [
            task("a", 4, status=TaskStatus.CANCELLED),
            task("b", 4),
        ]
        tasks[0].actual_start = at("2026-08-14 08:00")
        tasks[0].actual_end = at("2026-08-14 09:00")
        edges = [ScheduleEdge("a", "b")]
        result = forward_pass(tasks, edges, at("2026-08-14 12:00"))
        assert hhmm(result.start_of("b")) == "2026-08-14 12:00"

    def test_failed_task_with_continue_policy_is_settled(self):
        settled = task(
            "t", 4, status=TaskStatus.FAILED, on_failure=FailurePolicy.CONTINUE
        )
        blocked = task(
            "u", 4, status=TaskStatus.FAILED, on_failure=FailurePolicy.BLOCK
        )
        assert settled.is_settled
        assert not blocked.is_settled


class TestDeterminism:
    def test_repeated_runs_are_identical(self):
        tasks = [task("a", 2), task("b", 3), task("c", 4), task("z", 1)]
        edges = [
            ScheduleEdge("a", "b"),
            ScheduleEdge("a", "c"),
            ScheduleEdge("b", "z"),
            ScheduleEdge("c", "z"),
        ]
        first = backward_pass(tasks, edges, at("2026-08-15 18:00"))
        second = backward_pass(tasks, edges, at("2026-08-15 18:00"))
        assert first.intervals == second.intervals

    def test_unknown_calendar_falls_back_to_continuous(self):
        # Refusing to schedule an in-flight case would be worse than treating
        # a renamed calendar as 24x7.
        tasks = [task("t", 12, calendar="does_not_exist")]
        result = backward_pass(
            tasks, [], at("2026-08-15 18:00"), calendars=registry()
        )
        assert hhmm(result.start_of("t")) == "2026-08-15 06:00"
