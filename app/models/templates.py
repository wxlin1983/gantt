"""Calendars, task templates, gantt templates and their schedules.

Covers implement.md §3.2, §3.3, §3.4 and §4.16.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, JSONType, TimestampMixin, TZDateTime, enum_type
from .enums import ApiMode, FailurePolicy, ScheduleMode, TemplateStatus


class Calendar(Base):
    """Working-time definition (implement.md §3.2).

    Snapshotted into each case, because editing the holiday table later must
    not retroactively change how an existing case was scheduled.
    """

    __tablename__ = "calendars"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Taipei")
    #: {"mon": [["09:00", "18:00"]], ..., "sat": [], "sun": []}
    working_hours: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict
    )
    #: ["2026-01-01", "2026-02-16"]
    holidays: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)


class ApiCredential(Base):
    """Secrets referenced by ``auth_ref`` in a task template (§6.1.1).

    Templates store only the name, never the value, so exporting a template
    cannot leak a secret.
    """

    __tablename__ = "api_credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    encrypted_value: Mapped[str] = mapped_column(Text)
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now()
    )


class TaskTemplateRecord(TimestampMixin, Base):
    """A reusable task definition (implement.md §3.3)."""

    __tablename__ = "task_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: The DSL-level identifier, e.g. `bt1`.
    name: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(128), default="")
    duration_default: Mapped[str] = mapped_column(String(32), default="0S")
    schedule_mode: Mapped[ScheduleMode] = mapped_column(
        enum_type(ScheduleMode), default=ScheduleMode.CONTINUOUS
    )
    calendar_id: Mapped[int | None] = mapped_column(
        ForeignKey("calendars.id", ondelete="SET NULL")
    )
    para_schema: Mapped[list[Any]] = mapped_column(JSONType, default=list)

    task_api: Mapped[str | None] = mapped_column(String(128))
    api_mode: Mapped[ApiMode | None] = mapped_column(enum_type(ApiMode))
    api_config: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    api_timeout_s: Mapped[int] = mapped_column(Integer, default=1800)
    api_retry_max: Mapped[int] = mapped_column(Integer, default=3)
    api_retry_interval_s: Mapped[int] = mapped_column(Integer, default=300)
    api_poll_interval_s: Mapped[int] = mapped_column(Integer, default=60)

    allow_manual_override: Mapped[bool] = mapped_column(Boolean, default=True)
    on_failure: Mapped[FailurePolicy] = mapped_column(
        enum_type(FailurePolicy), default=FailurePolicy.BLOCK
    )
    warn_before_s: Mapped[int] = mapped_column(Integer, default=7200)
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )


class GanttTemplateRecord(Base):
    """A versioned flow definition (implement.md §3.4).

    Published versions are immutable; editing one means creating a new draft.
    """

    __tablename__ = "gantt_templates"
    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_template_version"),
        # At most one draft per template name, enforced in the database
        # rather than in the service layer.
        Index(
            "uq_gantt_template_draft",
            "name",
            unique=True,
            postgresql_where=text("status = 'draft'"),
            sqlite_where=text("status = 'draft'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[TemplateStatus] = mapped_column(
        enum_type(TemplateStatus), default=TemplateStatus.DRAFT
    )
    #: The complete DSL document, exactly as validated.
    definition: Mapped[dict[str, Any]] = mapped_column(JSONType)
    change_note: Mapped[str] = mapped_column(Text, default="")
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(TZDateTime)


class TemplateSchedule(Base):
    """Recurring case creation (implement.md §4.16).

    Kept out of ``definition`` so that publishing a new template version does
    not disturb the schedule's own state (last run, next run).
    """

    __tablename__ = "template_schedules"

    id: Mapped[int] = mapped_column(primary_key=True)
    template_name: Mapped[str] = mapped_column(String(128), unique=True)
    cron: Mapped[str] = mapped_column(String(64))
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Taipei")
    target_date_offset_s: Mapped[int] = mapped_column(Integer, default=0)
    name_template: Mapped[str] = mapped_column(String(256), default="")
    params: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    role_assignments: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    last_case_id: Mapped[int | None] = mapped_column(
        ForeignKey("gantt_cases.id", ondelete="SET NULL")
    )
    next_run_at: Mapped[datetime] = mapped_column(TZDateTime)
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
