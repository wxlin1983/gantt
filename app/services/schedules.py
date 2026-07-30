"""Recurring case creation (implement.md §4.16).

Three behaviours that look like omissions but are deliberate, and are surfaced
in the settings UI as such:

- the schedule follows the template's **latest published version**, because a
  recurring flow almost always wants the current definition
- a failure **disables the schedule** instead of retrying, since a schedule
  that fails every minute is worse than one that stopped
- missed occurrences are **not backfilled**: creating three cases that are
  overdue the moment they exist helps nobody
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import GanttCase, TemplateSchedule, User
from app.notifications import service as notifications
from app.services import cases as case_service
from app.services.cases import now_utc


class CronError(Exception):
    """The cron expression could not be parsed."""


def parse_cron(expression: str) -> tuple[set[int], ...]:
    """Parse a five-field cron expression into matchable sets.

    Supports ``*``, numbers, ``a-b`` ranges, ``a,b`` lists and ``*/n`` steps --
    the subset a scheduling UI actually generates. Anything fancier belongs in
    a real cron library, and the validation error says so.
    """
    fields = expression.split()
    if len(fields) != 5:
        raise CronError(
            f"expected 5 cron fields (minute hour day month weekday), "
            f"got {len(fields)}: {expression!r}"
        )
    bounds = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
    return tuple(
        _parse_field(field, low, high)
        for field, (low, high) in zip(fields, bounds, strict=True)
    )


def _parse_field(field: str, low: int, high: int) -> set[int]:
    values: set[int] = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, _, raw_step = part.partition("/")
            if not raw_step.isdigit() or int(raw_step) < 1:
                raise CronError(f"invalid step in cron field: {field!r}")
            step = int(raw_step)
        if part in ("*", ""):
            start, end = low, high
        elif "-" in part:
            begin, _, finish = part.partition("-")
            if not (begin.isdigit() and finish.isdigit()):
                raise CronError(f"invalid range in cron field: {field!r}")
            start, end = int(begin), int(finish)
        elif part.isdigit():
            start = end = int(part)
        else:
            raise CronError(f"unparseable cron field: {field!r}")
        if start < low or end > high or start > end:
            raise CronError(
                f"cron field {field!r} is outside {low}-{high}"
            )
        values.update(range(start, end + 1, step))
    return values


def matches(expression: str, moment: datetime) -> bool:
    minutes, hours, days, months, weekdays = parse_cron(expression)
    # cron weekdays are Sunday-based; Python's are Monday-based.
    weekday = (moment.weekday() + 1) % 7
    return (
        moment.minute in minutes
        and moment.hour in hours
        and moment.day in days
        and moment.month in months
        and weekday in weekdays
    )


def next_run_after(
    expression: str, after: datetime, timezone: str = "Asia/Taipei"
) -> datetime:
    """The next matching minute strictly after ``after``.

    Brute-forced a minute at a time over a two-year horizon: a schedule fires
    at most monthly, and a closed-form solution here would be a lot of subtle
    code for no measurable benefit.
    """
    zone = ZoneInfo(timezone)
    cursor = after.astimezone(zone).replace(second=0, microsecond=0)
    limit = cursor + timedelta(days=760)
    cursor += timedelta(minutes=1)
    while cursor <= limit:
        if matches(expression, cursor):
            return cursor.astimezone(after.tzinfo or zone)
        cursor += timedelta(minutes=1)
    raise CronError(
        f"{expression!r} does not fire within two years of {after.isoformat()}"
    )


async def upsert(
    session: AsyncSession,
    template_name: str,
    *,
    cron: str,
    timezone: str = "Asia/Taipei",
    target_date_offset_s: int = 0,
    name_template: str = "",
    params: dict | None = None,
    role_assignments: dict | None = None,
    enabled: bool = True,
    created_by_id: int | None = None,
) -> TemplateSchedule:
    # Validate before storing, so a bad expression fails at configuration time
    # rather than silently never firing.
    parse_cron(cron)
    row = (
        await session.scalars(
            select(TemplateSchedule).where(
                TemplateSchedule.template_name == template_name
            )
        )
    ).one_or_none()
    if row is None:
        row = TemplateSchedule(
            template_name=template_name, created_by_id=created_by_id
        )
        session.add(row)

    row.cron = cron
    row.timezone = timezone
    row.target_date_offset_s = target_date_offset_s
    row.name_template = name_template
    row.params = params or {}
    row.role_assignments = role_assignments or {}
    row.enabled = enabled
    row.next_run_at = next_run_after(cron, now_utc(), timezone)
    await session.flush()
    return row


def render_name(schedule: TemplateSchedule, moment: datetime) -> str:
    """Fill the name template's date placeholders."""
    local = moment.astimezone(ZoneInfo(schedule.timezone))
    if not schedule.name_template:
        return f"{schedule.template_name} {local:%Y-%m-%d}"
    return (
        schedule.name_template.replace("{{ now.year }}", str(local.year))
        .replace("{{ now.month }}", str(local.month))
        .replace("{{ now.day }}", str(local.day))
        .replace("{{年}}", str(local.year))
        .replace("{{月}}", str(local.month))
    )


async def run_due(session: AsyncSession) -> int:
    """Create cases for every schedule that is due."""
    moment = now_utc()
    rows = (
        await session.scalars(
            select(TemplateSchedule).where(
                TemplateSchedule.enabled.is_(True),
                TemplateSchedule.next_run_at <= moment,
            )
        )
    ).all()

    created = 0
    for schedule in rows:
        missed = _missed_occurrences(schedule, moment)
        try:
            case = await _create_from(session, schedule, moment)
        except Exception as exc:  # noqa: BLE001 - a bad schedule is data
            schedule.enabled = False
            await _report_failure(session, schedule, str(exc))
            await session.flush()
            continue

        schedule.last_run_at = moment
        schedule.last_case_id = case.id
        # Advance from now, not from the old next_run_at, so downtime does not
        # queue up a burst of backdated cases.
        schedule.next_run_at = next_run_after(
            schedule.cron, moment, schedule.timezone
        )
        await session.flush()
        created += 1

        if missed > 1:
            await _report_missed(session, schedule, missed - 1)
    return created


def _missed_occurrences(
    schedule: TemplateSchedule, moment: datetime
) -> int:
    """How many firings were due but never happened."""
    count = 0
    cursor = schedule.next_run_at
    while cursor <= moment and count < 500:
        count += 1
        try:
            cursor = next_run_after(
                schedule.cron, cursor, schedule.timezone
            )
        except CronError:
            break
    return count


async def _create_from(
    session: AsyncSession, schedule: TemplateSchedule, moment: datetime
) -> GanttCase:
    actor = (
        await session.scalars(
            select(User).where(User.id == schedule.created_by_id)
        )
    ).one_or_none()
    return await case_service.create(
        session,
        actor,
        name=render_name(schedule, moment),
        template_name=schedule.template_name,
        target_date=moment
        + timedelta(seconds=schedule.target_date_offset_s),
        params=dict(schedule.params or {}),
        role_assignments=dict(schedule.role_assignments or {}),
        # Keyed on the firing instant, so a retry or a second worker cannot
        # create the same occurrence twice.
        idempotency_key=(
            f"schedule:{schedule.id}:{schedule.next_run_at.isoformat()}"
        ),
    )


async def _report_failure(
    session: AsyncSession, schedule: TemplateSchedule, error: str
) -> None:
    title, body = notifications.describe(
        notifications.NotificationType.SCHEDULE_FAILED,
        case_name=schedule.template_name,
        error=error,
    )
    await notifications.notify(
        session,
        user_ids=[schedule.created_by_id],
        notification_type=notifications.NotificationType.SCHEDULE_FAILED,
        title=title,
        body=f"{body}\nThe schedule has been disabled.",
    )


async def _report_missed(
    session: AsyncSession, schedule: TemplateSchedule, missed: int
) -> None:
    await notifications.notify(
        session,
        user_ids=[schedule.created_by_id],
        notification_type=notifications.NotificationType.SCHEDULE_FAILED,
        title=f"Missed {missed} scheduled runs of {schedule.template_name}",
        body=(
            "Overdue occurrences are not backfilled, so only the current one "
            "was created."
        ),
    )
