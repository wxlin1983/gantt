"""SQLAlchemy models (implement.md §3).

Importing this package registers every table on ``Base.metadata``, which is
what Alembic autogeneration reads.
"""

from .activity import (
    AuditEvent,
    JobQueue,
    Notification,
    NotificationDelivery,
    NotificationPreference,
    TaskRun,
)
from .base import Base
from .cases import CaseTask, GanttCase, TaskDependency
from .enums import (
    ApiMode,
    CaseHealth,
    CaseStatus,
    CompletionSource,
    FailurePolicy,
    JobType,
    RunStatus,
    ScheduleMode,
    TaskStatus,
    TemplateStatus,
)
from .identity import Group, GroupMember, User
from .templates import (
    ApiCredential,
    Calendar,
    GanttTemplateRecord,
    TaskTemplateRecord,
    TemplateSchedule,
)

__all__ = [
    "ApiCredential",
    "ApiMode",
    "AuditEvent",
    "Base",
    "Calendar",
    "CaseHealth",
    "CaseStatus",
    "CaseTask",
    "CompletionSource",
    "FailurePolicy",
    "GanttCase",
    "GanttTemplateRecord",
    "Group",
    "GroupMember",
    "JobQueue",
    "JobType",
    "Notification",
    "NotificationDelivery",
    "NotificationPreference",
    "RunStatus",
    "ScheduleMode",
    "TaskDependency",
    "TaskRun",
    "TaskStatus",
    "TaskTemplateRecord",
    "TemplateSchedule",
    "TemplateStatus",
    "User",
]
