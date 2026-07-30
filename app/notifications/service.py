"""Notification creation and dispatch (implement.md §3.9, §6.6).

Deduplication is keyed on ``{user}:{scope}:{scope_id}:{type}:{epoch}``. Every
part of that matters:

- **user**, because one event often notifies several people, and leaving it out
  would swallow every recipient after the first
- **scope**, spelled out rather than inferred from whether ``case_task_id`` is
  null, because PostgreSQL's ``NULLS NOT DISTINCT`` would collide case-level
  notifications from different cases
- **epoch**, so a task that is reopened or retried can enter a fresh alert
  cycle instead of being permanently silent
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import (
    CaseTask,
    GanttCase,
    Notification,
    NotificationDelivery,
    NotificationPreference,
)


class NotificationType:
    TASK_READY = "task.ready"
    TASK_UNASSIGNED = "task.unassigned"
    TASK_DUE_SOON = "task.due_soon"
    TASK_LATE_START = "task.late_start"
    TASK_OVERDUE = "task.overdue"
    TASK_FAILED = "task.failed"
    TASK_ASSIGNED = "task.assigned"
    OPTIONAL_CANCELLED = "task.optional_cancelled"
    CASE_OVERDUE = "case.overdue"
    CASE_COMPLETED = "case.completed"
    SCHEDULE_FAILED = "schedule.failed"


#: Types that should reach the user once and only once per epoch.
_DEDUPED = {
    NotificationType.TASK_READY,
    NotificationType.TASK_DUE_SOON,
    NotificationType.TASK_LATE_START,
    NotificationType.TASK_OVERDUE,
    NotificationType.TASK_FAILED,
    NotificationType.TASK_UNASSIGNED,
    NotificationType.CASE_OVERDUE,
}


def dedup_key(
    user_id: int,
    notification_type: str,
    *,
    task_id: int | None = None,
    case_id: int | None = None,
    epoch: int = 0,
) -> str | None:
    if notification_type not in _DEDUPED:
        return None
    scope, scope_id = (
        ("task", task_id) if task_id is not None else ("case", case_id)
    )
    return f"{user_id}:{scope}:{scope_id}:{notification_type}:{epoch}"


class Channel(Protocol):
    """An outbound delivery route.

    Same shape as a task handler on purpose: adding Slack should be one new
    file, not a change to the notification core.
    """

    name: str

    async def send(
        self, notification: Notification, recipient_email: str
    ) -> None: ...


@dataclass(slots=True)
class LoggingEmailChannel:
    """Stand-in for SMTP.

    Writes the message to the delivery table instead of sending it, so the
    pipeline is exercised end to end without an outbound mail server.
    """

    name: str = "email"
    sent: list[tuple[str, str]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.sent is None:
            self.sent = []

    async def send(
        self, notification: Notification, recipient_email: str
    ) -> None:
        self.sent.append((recipient_email, notification.title))


_channels: dict[str, Channel] = {}


def register_channel(channel: Channel) -> None:
    _channels[channel.name] = channel


def channels() -> dict[str, Channel]:
    if not _channels:
        register_channel(LoggingEmailChannel())
    return _channels


async def notify(
    session: AsyncSession,
    *,
    user_ids: Iterable[int],
    notification_type: str,
    title: str,
    body: str = "",
    case: GanttCase | None = None,
    task: CaseTask | None = None,
    epoch: int = 0,
) -> list[Notification]:
    """Create in-app notifications and queue outbound deliveries.

    Duplicates are absorbed rather than raised: a scan that runs every five
    minutes will legitimately try to re-notify, and that is not an error.
    """
    created: list[Notification] = []
    for user_id in {uid for uid in user_ids if uid}:
        key = dedup_key(
            user_id,
            notification_type,
            task_id=task.id if task else None,
            case_id=case.id if case else None,
            epoch=epoch,
        )
        if key is not None and await _exists(session, key):
            continue

        notification = Notification(
            user_id=user_id,
            type=notification_type,
            title=title,
            body=body,
            case_id=case.id if case else None,
            case_task_id=task.id if task else None,
            dedup_key=key,
        )
        session.add(notification)
        try:
            await session.flush()
        except IntegrityError:
            # Another worker won the race; its copy is just as good as ours.
            await session.rollback()
            continue

        created.append(notification)
        await _queue_deliveries(session, notification)
    return created


async def _exists(session: AsyncSession, key: str) -> bool:
    found = await session.scalars(
        select(Notification.id).where(Notification.dedup_key == key)
    )
    return found.first() is not None


async def _queue_deliveries(
    session: AsyncSession, notification: Notification
) -> None:
    """Record a pending delivery per channel the user wants for this type.

    In-app is always on, so an absent preference row means "in-app only"
    rather than "nothing".
    """
    preference = (
        await session.scalars(
            select(NotificationPreference).where(
                NotificationPreference.user_id == notification.user_id,
                NotificationPreference.type == notification.type,
            )
        )
    ).one_or_none()
    wanted = (
        preference.channels
        if preference is not None
        else get_settings().notification_channels
    )
    for name in wanted or ():
        if name in channels():
            session.add(
                NotificationDelivery(
                    notification_id=notification.id,
                    channel=name,
                    status="pending",
                )
            )
    await session.flush()


async def flush_deliveries(session: AsyncSession, limit: int = 50) -> int:
    """Send whatever is pending. Failures are recorded, never fatal.

    A notification that cannot be emailed is still visible in-app, so a broken
    mail server must not hold up the flow it was reporting on.
    """
    from app.models import User

    rows = (
        await session.execute(
            select(NotificationDelivery, Notification, User.email)
            .join(
                Notification,
                Notification.id == NotificationDelivery.notification_id,
            )
            .join(User, User.id == Notification.user_id)
            .where(NotificationDelivery.status == "pending")
            .limit(limit)
        )
    ).all()

    sent = 0
    for delivery, notification, email in rows:
        channel = channels().get(delivery.channel)
        if channel is None:
            delivery.status = "failed"
            delivery.error_message = f"channel {delivery.channel} not enabled"
            continue
        try:
            await channel.send(notification, email)
            delivery.status = "sent"
            sent += 1
        except Exception as exc:  # noqa: BLE001 - channel errors are data
            delivery.status = "failed"
            delivery.error_message = str(exc)[:500]
    await session.flush()
    return sent


def describe(notification_type: str, **context: Any) -> tuple[str, str]:
    """Render a title and body for a notification type."""
    task = context.get("task_name", "")
    case = context.get("case_name", "")
    match notification_type:
        case NotificationType.TASK_READY:
            return (f"Ready to start: {task}", f"in {case}")
        case NotificationType.TASK_LATE_START:
            return (
                f"Not started yet: {task}",
                f"{task} in {case} should already have begun",
            )
        case NotificationType.TASK_DUE_SOON:
            return (f"Due soon: {task}", f"in {case}")
        case NotificationType.TASK_OVERDUE:
            return (f"Overdue: {task}", f"in {case}")
        case NotificationType.TASK_FAILED:
            return (
                f"Failed: {task}",
                context.get("error", f"{task} in {case} failed"),
            )
        case NotificationType.TASK_UNASSIGNED:
            return (
                f"Unassigned and ready: {task}",
                f"{task} in {case} has no owner",
            )
        case NotificationType.OPTIONAL_CANCELLED:
            return (
                f"Closed out: {task}",
                f"{case} finished, so the optional step {task} was cancelled",
            )
        case NotificationType.CASE_OVERDUE:
            return (
                f"Forecast past target: {case}",
                context.get("detail", ""),
            )
        case NotificationType.CASE_COMPLETED:
            return (f"Completed: {case}", "")
        case NotificationType.SCHEDULE_FAILED:
            return (
                f"Recurring creation failed: {case}",
                context.get("error", ""),
            )
    return (notification_type, "")
