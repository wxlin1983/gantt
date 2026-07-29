"""Scheduling engine (implement.md §5).

Pure functions over plain data: the passes never touch the database, so case
creation, the dry-run preview and the what-if simulation all run the same code
and cannot disagree about what a schedule looks like.
"""

from .adapters import apply_baseline, from_expansion
from .analysis import (
    Outlook,
    blocking_failures,
    critical_path,
    evaluate,
    late_starts,
    progress_ratio,
)
from .calendars import (
    CONTINUOUS,
    BusinessCalendar,
    Calendar,
    CalendarError,
    ContinuousCalendar,
    office_calendar,
    registry,
)
from .passes import backward_pass, forward_pass, latest_finish
from .types import (
    Interval,
    Schedule,
    ScheduleEdge,
    ScheduleTask,
    calendar_for,
    effective_seconds,
    remaining_seconds,
)

__all__ = [
    "CONTINUOUS",
    "BusinessCalendar",
    "Calendar",
    "CalendarError",
    "ContinuousCalendar",
    "Interval",
    "Outlook",
    "Schedule",
    "ScheduleEdge",
    "ScheduleTask",
    "apply_baseline",
    "backward_pass",
    "blocking_failures",
    "calendar_for",
    "critical_path",
    "effective_seconds",
    "evaluate",
    "forward_pass",
    "from_expansion",
    "late_starts",
    "latest_finish",
    "office_calendar",
    "progress_ratio",
    "registry",
    "remaining_seconds",
]
