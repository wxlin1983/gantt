"""Cases, their tasks and the dependency edges (implement.md §3.5-§3.7)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, JSONType, TZDateTime
from .enums import (
    ApiMode,
    CaseHealth,
    CaseStatus,
    CompletionSource,
    FailurePolicy,
    ScheduleMode,
    TaskStatus,
)


class GanttCase(Base):
    """One run of a flow (implement.md §3.5)."""

    __tablename__ = "gantt_cases"
    __table_args__ = (
        Index("idx_cases_status_target", "status", "target_date"),
        Index(
            "idx_cases_health",
            "health",
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(256))
    template_name: Mapped[str] = mapped_column(String(128))
    template_version: Mapped[int] = mapped_column(Integer)

    #: Self-contained copy of the template, its task templates and the
    #: calendars they use. Immutable: later template edits must never change
    #: how a running case was planned.
    template_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONType)
    params: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    role_assignments: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict
    )
    #: Tasks that `when` filtered out, kept so the UI can explain the absence.
    skipped_tasks: Mapped[list[Any]] = mapped_column(JSONType, default=list)

    target_date: Mapped[datetime] = mapped_column(TZDateTime)
    #: Backward pass starts from target_date minus this (§5.8).
    buffer_seconds: Mapped[int] = mapped_column(Integer, default=0)
    target_date_history: Mapped[list[Any]] = mapped_column(
        JSONType, default=list
    )
    baseline_resets: Mapped[list[Any]] = mapped_column(JSONType, default=list)

    status: Mapped[CaseStatus] = mapped_column(
        String(16), default=CaseStatus.ACTIVE
    )
    forecast_end: Mapped[datetime | None] = mapped_column(TZDateTime)
    health: Mapped[CaseHealth | None] = mapped_column(String(16))
    buffer_consumed_ratio: Mapped[float | None] = mapped_column(Numeric(5, 4))
    progress_ratio: Mapped[float | None] = mapped_column(Numeric(5, 4))

    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    #: Client-supplied; stops a double submit creating two cases (§8.2).
    idempotency_key: Mapped[str | None] = mapped_column(
        String(128), unique=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    archived_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    #: Optimistic lock; a stale write is rejected rather than silently merged.
    version: Mapped[int] = mapped_column(Integer, default=1)

    tasks: Mapped[list[CaseTask]] = relationship(
        back_populates="case", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<GanttCase {self.id} {self.name!r}>"


class CaseTask(Base):
    """One task inside a case (implement.md §3.6)."""

    __tablename__ = "case_tasks"
    __table_args__ = (
        UniqueConstraint("case_id", "name", name="uq_case_task_name"),
        Index("idx_case_tasks_case", "case_id"),
        Index("idx_case_tasks_owner_status", "owner_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(
        ForeignKey("gantt_cases.id", ondelete="CASCADE")
    )
    #: Unique within the case.
    name: Mapped[str] = mapped_column(String(128))
    display_name: Mapped[str] = mapped_column(String(256), default="")
    source_task_template: Mapped[str | None] = mapped_column(String(64))
    phase: Mapped[str] = mapped_column(String(128), default="")

    duration_seconds: Mapped[int] = mapped_column(Integer, default=0)
    schedule_mode: Mapped[ScheduleMode] = mapped_column(
        String(16), default=ScheduleMode.CONTINUOUS
    )
    calendar_id: Mapped[int | None] = mapped_column(
        ForeignKey("calendars.id", ondelete="SET NULL")
    )

    #: Nullable on purpose: a task inserted after the case was created has no
    #: original plan, and inventing one would produce meaningless variance
    #: numbers (§5.10).
    baseline_start: Mapped[datetime | None] = mapped_column(TZDateTime)
    baseline_end: Mapped[datetime | None] = mapped_column(TZDateTime)

    forecast_start: Mapped[datetime | None] = mapped_column(TZDateTime)
    forecast_end: Mapped[datetime | None] = mapped_column(TZDateTime)
    actual_start: Mapped[datetime | None] = mapped_column(TZDateTime)
    actual_end: Mapped[datetime | None] = mapped_column(TZDateTime)

    status: Mapped[TaskStatus] = mapped_column(
        String(16), default=TaskStatus.PENDING
    )
    completion_source: Mapped[CompletionSource | None] = mapped_column(
        String(16)
    )
    completed_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    completion_note: Mapped[str] = mapped_column(Text, default="")

    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    #: How the owner was derived: 'role:pm', 'group_lead:qa', 'same_as:x',
    #: 'literal', or 'manual' once a person has overridden it. Reassigning a
    #: role in bulk must not clobber a manual choice.
    owner_source: Mapped[str] = mapped_column(String(128), default="literal")
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("groups.id", ondelete="SET NULL")
    )
    params: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)

    task_api: Mapped[str | None] = mapped_column(String(128))
    api_mode: Mapped[ApiMode | None] = mapped_column(String(32))
    api_config: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    allow_manual_override: Mapped[bool] = mapped_column(Boolean, default=True)

    on_failure: Mapped[FailurePolicy] = mapped_column(
        String(16), default=FailurePolicy.BLOCK
    )
    is_optional: Mapped[bool] = mapped_column(Boolean, default=False)
    warn_before_seconds: Mapped[int] = mapped_column(Integer, default=7200)

    is_on_critical_path: Mapped[bool] = mapped_column(Boolean, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now()
    )
    version: Mapped[int] = mapped_column(Integer, default=1)

    case: Mapped[GanttCase] = relationship(back_populates="tasks")

    @property
    def is_unplanned(self) -> bool:
        """True for tasks added after the case was created."""
        return self.baseline_start is None

    def __repr__(self) -> str:
        return f"<CaseTask {self.name} {self.status}>"


class TaskDependency(Base):
    """A DAG edge (implement.md §3.7).

    Stored as its own table rather than an array column because both
    directions are queried hot: the backward pass walks successors, the
    forward pass walks predecessors.
    """

    __tablename__ = "task_dependencies"
    __table_args__ = (
        UniqueConstraint(
            "predecessor_id", "successor_id", name="uq_dependency_pair"
        ),
        CheckConstraint(
            "predecessor_id <> successor_id", name="no_self_dependency"
        ),
        CheckConstraint("lag_seconds >= 0", name="non_negative_lag"),
        Index("idx_deps_pred", "predecessor_id"),
        Index("idx_deps_succ", "successor_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(
        ForeignKey("gantt_cases.id", ondelete="CASCADE")
    )
    predecessor_id: Mapped[int] = mapped_column(
        ForeignKey("case_tasks.id", ondelete="CASCADE")
    )
    successor_id: Mapped[int] = mapped_column(
        ForeignKey("case_tasks.id", ondelete="CASCADE")
    )
    #: Wait time after the predecessor finishes; accumulates when a skipped
    #: node is bypassed.
    lag_seconds: Mapped[int] = mapped_column(Integer, default=0)
