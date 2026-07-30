"""Runtime state enums.

The DSL enums (schedule mode, failure policy, api mode) are reused from
``app.dsl.schema`` so a value never has two definitions.
"""

from __future__ import annotations

from enum import StrEnum

from app.dsl.schema import ApiMode, FailurePolicy, ScheduleMode

__all__ = [
    "ApiMode",
    "CaseHealth",
    "CaseStatus",
    "FailurePolicy",
    "JobType",
    "RunStatus",
    "ScheduleMode",
    "TaskStatus",
    "TemplateStatus",
]


class TemplateStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class CaseStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class CaseHealth(StrEnum):
    ON_TRACK = "on_track"
    AT_RISK = "at_risk"
    OVERDUE = "overdue"


class TaskStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    def is_settled(self, on_failure: FailurePolicy | str) -> bool:
        """Whether downstream tasks may proceed (implement.md §4.13).

        The gate is "settled", not "done": a cancelled task, or a failed one
        whose policy is `continue`, also releases its successors.

        Compared by value rather than identity on purpose. These are
        ``StrEnum``, so a caller passing a plain ``"continue"`` -- out of JSON,
        out of a fixture, out of a column that lost its type -- still gets the
        right answer instead of silently taking the blocking branch.
        """
        if self in (TaskStatus.DONE, TaskStatus.CANCELLED):
            return True
        return (
            self == TaskStatus.FAILED
            and on_failure == FailurePolicy.CONTINUE
        )


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class JobType(StrEnum):
    TRIGGER = "trigger"
    POLL = "poll"
    TIMEOUT_CHECK = "timeout_check"
    RECALC = "recalc"
    DEADLINE_SCAN = "deadline_scan"
    SCHEDULE_SCAN = "schedule_scan"


class CompletionSource(StrEnum):
    MANUAL = "manual"
    API = "api"
