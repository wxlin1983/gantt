"""Template expansion feeding the scheduling engine end to end."""

from __future__ import annotations

from app.dsl.expansion import expand
from app.dsl.loader import parse_gantt_template, parse_task_template
from app.scheduling import (
    apply_baseline,
    backward_pass,
    evaluate,
    from_expansion,
    office_calendar,
    registry,
)

from .conftest import at, hhmm

TEMPLATE = """
gantt:
  template_name: launch
  buffer: 8H
  template_para:
    - para_name: test_hours
      para_type: int
      para_default: 12
  flow:
    - id: prep
      uses: tt
      duration: 12H
      requirement: none
    - id: test
      uses: tt
      duration: "{{ para.test_hours }}H"
      requirement:
        - task: prep
          lag: 4H
    - id: review
      uses: tt
      duration: 6H
      when: "{{ para.test_hours > 8 }}"
      requirement: prep
      schedule_mode: business
      calendar: taiwan_office
    - id: report
      uses: tt
      duration: 12H
      requirement: [test, review]
"""


def build(params=None):
    template = parse_gantt_template(TEMPLATE)
    task_templates = {
        "tt": parse_task_template({"id": "tt", "default_duration": "1H"})
    }
    return expand(template, task_templates, params=params or {})


class TestFromExpansion:
    def test_carries_duration_calendar_and_flags(self):
        tasks, edges = from_expansion(build())
        by_id = {task.id: task for task in tasks}

        assert by_id["prep"].duration_seconds == 12 * 3600
        assert by_id["test"].duration_seconds == 12 * 3600
        assert by_id["review"].calendar == "taiwan_office"
        assert {
            (edge.predecessor, edge.successor, edge.lag_seconds)
            for edge in edges
        } == {
            ("prep", "test", 4 * 3600),
            ("prep", "review", 0),
            ("test", "report", 0),
            ("review", "report", 0),
        }

    def test_parameters_change_the_schedule(self):
        short, _ = from_expansion(build({"test_hours": 4}))
        long, _ = from_expansion(build({"test_hours": 40}))
        assert {t.id for t in short} == {"prep", "test", "report"}
        # The review step only exists above the threshold
        assert "review" in {t.id for t in long}

    def test_buffer_reaches_the_engine(self):
        result = build()
        assert result.buffer_seconds == 8 * 3600


class TestEndToEnd:
    def test_backward_pass_over_an_expanded_template(self):
        result = build({"test_hours": 12})
        tasks, edges = from_expansion(result)
        calendars = registry(office_calendar())
        target = at("2026-08-21 18:00")

        baseline = backward_pass(
            tasks,
            edges,
            target,
            buffer_seconds=result.buffer_seconds,
            calendars=calendars,
        )
        apply_baseline(tasks, baseline.intervals)

        # The plan ends a buffer's width before the target
        assert hhmm(baseline.end_of("report")) == "2026-08-21 10:00"
        # report waits for both branches
        assert baseline.start_of("report") >= baseline.end_of("test")
        assert baseline.start_of("report") >= baseline.end_of("review")
        # every task picked up its baseline
        assert all(task.baseline_start is not None for task in tasks)

    def test_outlook_on_a_fresh_case(self):
        result = build({"test_hours": 12})
        tasks, edges = from_expansion(result)
        calendars = registry(office_calendar())
        target = at("2026-08-28 18:00")

        baseline = backward_pass(
            tasks,
            edges,
            target,
            buffer_seconds=result.buffer_seconds,
            calendars=calendars,
        )
        apply_baseline(tasks, baseline.intervals)

        outlook = evaluate(
            tasks,
            edges,
            target,
            at("2026-08-17 09:00"),
            buffer_seconds=result.buffer_seconds,
            calendars=calendars,
        )
        assert outlook.progress_ratio == 0.0
        assert outlook.forecast_end < target
        assert outlook.critical_path
        assert outlook.late_starts == []

    def test_the_engine_is_deterministic_over_expansion(self):
        calendars = registry(office_calendar())
        target = at("2026-08-21 18:00")
        runs = []
        for _ in range(2):
            tasks, edges = from_expansion(build({"test_hours": 12}))
            runs.append(
                backward_pass(
                    tasks, edges, target, calendars=calendars
                ).intervals
            )
        assert runs[0] == runs[1]
