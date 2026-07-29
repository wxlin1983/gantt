"""Building engine input from other layers.

The engine deliberately knows nothing about the DSL or the ORM. These
adapters are the only place the shapes meet, so a change to either side has
one obvious place to be reconciled.
"""

from __future__ import annotations

from app.dsl.schema import ExpansionResult

from .types import ScheduleEdge, ScheduleTask


def from_expansion(
    result: ExpansionResult,
) -> tuple[list[ScheduleTask], list[ScheduleEdge]]:
    """Convert freshly expanded template output into engine input.

    Used at case creation and by the dry-run preview. Every task is pending
    with no actual timestamps, which is exactly what the backward pass wants.
    """
    tasks = [
        ScheduleTask(
            id=task.id,
            duration_seconds=task.duration_seconds,
            duration_days=task.duration_days,
            calendar=task.calendar,
            on_failure=task.on_failure,
            is_optional=task.optional,
        )
        for task in result.tasks
    ]
    edges = [
        ScheduleEdge(
            predecessor=edge.predecessor,
            successor=edge.successor,
            lag_seconds=edge.lag_seconds,
        )
        for edge in result.edges
    ]
    return tasks, edges


def apply_baseline(
    tasks: list[ScheduleTask], baseline: dict[str, tuple]
) -> None:
    """Copy a computed baseline back onto the tasks, in place.

    The forward pass needs each task's baseline start as the floor for a root
    task that has not begun yet, so the two passes are usually run back to
    back over the same task objects.
    """
    for task in tasks:
        interval = baseline.get(task.id)
        if interval is None:
            continue
        task.baseline_start, task.baseline_end = interval
