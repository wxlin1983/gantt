"""Execution records, the job queue, audit trail and notifications.

Covers implement.md §3.8 and §3.9.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
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

from .base import Base, JSONType, TZDateTime, enum_type
from .enums import JobType, RunStatus


class TaskRun(Base):
    """One attempt at driving a task through its API (§3.8)."""

    __tablename__ = "task_runs"
    __table_args__ = (
        UniqueConstraint("case_task_id", "attempt", name="uq_run_attempt"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    case_task_id: Mapped[int] = mapped_column(
        ForeignKey("case_tasks.id", ondelete="CASCADE")
    )
    attempt: Mapped[int] = mapped_column(Integer)
    handler_name: Mapped[str] = mapped_column(String(128))
    status: Mapped[RunStatus] = mapped_column(enum_type(RunStatus))
    request_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict
    )
    response_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    #: Identifier handed back by the external system, used when polling.
    external_ref: Mapped[str | None] = mapped_column(String(256))
    error_message: Mapped[str | None] = mapped_column(Text)
    error_detail: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime)


class JobQueue(Base):
    """Work items for the worker pool (§3.8).

    Deliberately a table rather than a broker: keeping the queue in the same
    transaction as task state removes a whole class of "job says done, row
    says running" inconsistencies, and one fewer service to operate.
    """

    __tablename__ = "job_queue"
    __table_args__ = (
        Index(
            "idx_job_queue_pickup",
            "run_after",
            postgresql_where=text("locked_by IS NULL"),
            sqlite_where=text("locked_by IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_type: Mapped[JobType] = mapped_column(enum_type(JobType))
    case_task_id: Mapped[int | None] = mapped_column(
        ForeignKey("case_tasks.id", ondelete="CASCADE")
    )
    case_id: Mapped[int | None] = mapped_column(
        ForeignKey("gantt_cases.id", ondelete="CASCADE")
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    run_after: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now()
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    #: Set by SELECT ... FOR UPDATE SKIP LOCKED; reclaimed if it goes stale.
    locked_by: Mapped[str | None] = mapped_column(String(128))
    locked_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now()
    )


class AuditEvent(Base):
    """Append-only record of who changed what (§3.9)."""

    __tablename__ = "audit_events"
    __table_args__ = (Index("idx_audit_case", "case_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int | None] = mapped_column(
        ForeignKey("gantt_cases.id", ondelete="CASCADE")
    )
    case_task_id: Mapped[int | None] = mapped_column(
        ForeignKey("case_tasks.id", ondelete="CASCADE")
    )
    #: NULL means the system acted, not a person.
    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    event_type: Mapped[str] = mapped_column(String(64))
    before_state: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    after_state: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now()
    )


class Notification(Base):
    """An in-app notification (§3.9)."""

    __tablename__ = "notifications"
    __table_args__ = (
        # Partial unique index rather than a plain constraint: only rows that
        # opt into deduplication participate.
        Index(
            "uq_notif_dedup",
            "dedup_key",
            unique=True,
            postgresql_where=text("dedup_key IS NOT NULL"),
            sqlite_where=text("dedup_key IS NOT NULL"),
        ),
        Index(
            "idx_notif_unread",
            "user_id",
            "created_at",
            postgresql_where=text("read_at IS NULL"),
            sqlite_where=text("read_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    type: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(256))
    body: Mapped[str] = mapped_column(Text, default="")
    case_id: Mapped[int | None] = mapped_column(
        ForeignKey("gantt_cases.id", ondelete="CASCADE")
    )
    case_task_id: Mapped[int | None] = mapped_column(
        ForeignKey("case_tasks.id", ondelete="CASCADE")
    )
    #: "{user_id}:{scope}:{scope_id}:{type}:{epoch}"; NULL disables dedup.
    #: The user id must be part of it, otherwise notifying a second recipient
    #: of the same event would be swallowed as a duplicate.
    dedup_key: Mapped[str | None] = mapped_column(String(256))
    read_at: Mapped[datetime | None] = mapped_column(TZDateTime)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now()
    )


class NotificationDelivery(Base):
    """Outbound delivery attempt for one channel (§3.9)."""

    __tablename__ = "notification_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "notification_id", "channel", name="uq_delivery_channel"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    notification_id: Mapped[int] = mapped_column(
        ForeignKey("notifications.id", ondelete="CASCADE")
    )
    channel: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    error_message: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(TZDateTime)


class NotificationPreference(Base):
    """Per-user channel choice for one notification type (§3.9).

    In-app delivery is always on; this table only governs outbound channels.
    """

    __tablename__ = "notification_preferences"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    type: Mapped[str] = mapped_column(String(64), primary_key=True)
    channels: Mapped[list[Any]] = mapped_column(JSONType, default=list)
