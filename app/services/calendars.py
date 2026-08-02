"""Loading calendars out of the database and out of case snapshots.

Two sources, one shape. Live editing reads the ``calendars`` table; a running
case reads the copy frozen into its snapshot, because changing the holiday
table later must not retroactively move an existing case's dates (§4.8).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import date
from typing import Any
from zoneinfo import ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Calendar as CalendarRow
from app.models import GanttTemplateRecord, TaskTemplateRecord
from app.scheduling import (
    CONTINUOUS,
    BusinessCalendar,
    Calendar,
)

BUILTIN_CONTINUOUS = "continuous"

#: Seeded on an empty database so a template can name a business calendar
#: without an administrator having to create one first.
DEFAULT_OFFICE = {
    "name": "taiwan_office",
    "timezone": "Asia/Taipei",
    "working_hours": {
        "mon": [["09:00", "18:00"]],
        "tue": [["09:00", "18:00"]],
        "wed": [["09:00", "18:00"]],
        "thu": [["09:00", "18:00"]],
        "fri": [["09:00", "18:00"]],
        "sat": [],
        "sun": [],
    },
    "holidays": [],
}


def build(definitions: dict[str, dict[str, Any]]) -> dict[str, Calendar]:
    """Turn ``{name: definition}`` into a usable registry.

    ``continuous`` is always present and always wins: it is the DSL default,
    and a database row must not be able to redefine what 24x7 means.
    """
    result: dict[str, Calendar] = {}
    for name, definition in definitions.items():
        if name == BUILTIN_CONTINUOUS:
            continue
        result[name] = BusinessCalendar(
            name,
            working_hours=definition.get("working_hours") or {},
            holidays=definition.get("holidays") or (),
            timezone=definition.get("timezone") or "Asia/Taipei",
        )
    result[CONTINUOUS.name] = CONTINUOUS
    return result


def definitions_from_rows(rows: list[CalendarRow]) -> dict[str, dict]:
    """Extract the snapshot-shaped definition from ORM rows."""
    return {
        row.name: {
            "timezone": row.timezone,
            "working_hours": row.working_hours or {},
            "holidays": row.holidays or [],
        }
        for row in rows
    }


async def load_all(session: AsyncSession) -> dict[str, Calendar]:
    """Every calendar currently defined, for template preview and editing."""
    rows = (await session.scalars(select(CalendarRow))).all()
    return build(definitions_from_rows(list(rows)))


async def load_definitions(session: AsyncSession) -> dict[str, dict]:
    """Definitions rather than instances, for freezing into a snapshot."""
    rows = (await session.scalars(select(CalendarRow))).all()
    return definitions_from_rows(list(rows))


def from_snapshot(snapshot: dict[str, Any]) -> dict[str, Calendar]:
    """Rebuild the registry a case was originally scheduled against."""
    return build(snapshot.get("calendars") or {})


async def ensure_builtins(session: AsyncSession) -> None:
    """Seed the calendars a fresh installation needs.

    ``continuous`` gets a row purely so administrators can see it in the UI;
    the engine uses the built-in instance regardless.
    """
    existing = set(
        (await session.scalars(select(CalendarRow.name))).all()
    )
    if BUILTIN_CONTINUOUS not in existing:
        session.add(
            CalendarRow(
                name=BUILTIN_CONTINUOUS,
                timezone="UTC",
                working_hours={},
                holidays=[],
                is_builtin=True,
            )
        )
    if DEFAULT_OFFICE["name"] not in existing:
        session.add(CalendarRow(is_builtin=True, **DEFAULT_OFFICE))
    await session.flush()


# --- administration --------------------------------------------------------
#
# Everything above reads calendars. Everything below maintains them, which
# until now nothing could: no API, no CLI, no import path. `taiwan_office`
# shipped with an empty holiday list, so every business-mode task scheduled
# straight through every public holiday -- the arithmetic was exact and its
# input was empty.


class CalendarError(Exception):
    """A calendar could not be saved."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _normalise_holidays(raw: Iterable[str] | None) -> list[str]:
    """Parse, de-duplicate and sort.

    Stored sorted so a diff between two saves is about the dates rather than
    the order somebody happened to paste them in.
    """
    if raw is None:
        return []
    seen: set[date] = set()
    for entry in raw:
        text = str(entry).strip()
        if not text:
            continue
        try:
            seen.add(date.fromisoformat(text))
        except ValueError as exc:
            raise CalendarError(
                "E_BAD_HOLIDAY", f"`{text}` is not a date (use YYYY-MM-DD)"
            ) from exc
    return [day.isoformat() for day in sorted(seen)]


def _validate(
    name: str,
    timezone: str,
    working_hours: dict[str, Any],
    holidays: list[str],
) -> BusinessCalendar:
    """Build the real thing, and let it be the judge.

    Every rule -- unknown weekday key, a window that ends before it starts,
    two windows that overlap, an unknown zone -- is already enforced by the
    constructor the engine uses. Re-stating them here would create two
    definitions of a valid calendar that could drift apart.
    """
    try:
        return BusinessCalendar(
            name,
            working_hours=working_hours or {},
            holidays=holidays,
            timezone=timezone,
        )
    except ZoneInfoNotFoundError as exc:
        raise CalendarError(
            "E_BAD_TIMEZONE", f"`{timezone}` is not a known timezone"
        ) from exc
    except ValueError as exc:
        raise CalendarError("E_BAD_WORKING_HOURS", str(exc)) from exc


def day_seconds_of(row: CalendarRow) -> int:
    """What ``1D`` means on this calendar (§4.5).

    Surfaced because a `D` duration under a business calendar resolves against
    it, and that conversion has been a real source of error.
    """
    if row.name == BUILTIN_CONTINUOUS:
        return CONTINUOUS.day_seconds
    try:
        return _validate(
            row.name, row.timezone, row.working_hours or {}, []
        ).day_seconds
    except CalendarError:
        # A row that no longer validates should still be listable, so it can
        # be seen and repaired rather than breaking the page it appears on.
        return 0


async def list_rows(session: AsyncSession) -> list[CalendarRow]:
    return list(
        (await session.scalars(select(CalendarRow).order_by(CalendarRow.name)))
        .unique()
        .all()
    )


async def get(session: AsyncSession, calendar_id: int) -> CalendarRow:
    row = (
        await session.scalars(
            select(CalendarRow).where(CalendarRow.id == calendar_id)
        )
    ).one_or_none()
    if row is None:
        raise CalendarError(
            "E_CALENDAR_NOT_FOUND", f"no calendar {calendar_id}"
        )
    return row


async def create(
    session: AsyncSession,
    *,
    name: str,
    timezone: str = "Asia/Taipei",
    working_hours: dict[str, Any] | None = None,
    holidays: Iterable[str] | None = None,
) -> CalendarRow:
    name = name.strip()
    if not name:
        raise CalendarError("E_BAD_CALENDAR_NAME", "name must not be empty")
    if name == BUILTIN_CONTINUOUS:
        raise CalendarError(
            "E_RESERVED_NAME",
            "`continuous` is built in and cannot be redefined",
        )
    clash = (
        await session.scalars(
            select(CalendarRow).where(CalendarRow.name == name)
        )
    ).one_or_none()
    if clash is not None:
        raise CalendarError(
            "E_DUPLICATE_CALENDAR", f"`{name}` already exists"
        )

    days = _normalise_holidays(holidays)
    _validate(name, timezone, working_hours or {}, days)
    row = CalendarRow(
        name=name,
        timezone=timezone,
        working_hours=working_hours or {},
        holidays=days,
        is_builtin=False,
    )
    session.add(row)
    await session.flush()
    return row


async def update(
    session: AsyncSession,
    row: CalendarRow,
    *,
    timezone: str | None = None,
    working_hours: dict[str, Any] | None = None,
    holidays: Iterable[str] | None = None,
) -> CalendarRow:
    """Edit working time.

    `name` is deliberately not editable: templates refer to a calendar by it,
    and `calendar_for` falls back to 24x7 for a name it does not recognise
    without complaining, so a rename would silently reschedule every task that
    used the old one.

    Builtins *are* editable. Adding holidays to `taiwan_office` is the whole
    point; only deletion is refused.
    """
    if row.name == BUILTIN_CONTINUOUS:
        raise CalendarError(
            "E_READ_ONLY_CALENDAR",
            "`continuous` means 24x7 and the engine ignores this row",
        )

    days = (
        list(row.holidays or [])
        if holidays is None
        else _normalise_holidays(holidays)
    )
    hours = row.working_hours or {} if working_hours is None else working_hours
    _validate(
        row.name,
        row.timezone if timezone is None else timezone,
        hours,
        days,
    )
    if timezone is not None:
        row.timezone = timezone
    if working_hours is not None:
        row.working_hours = working_hours
    row.holidays = days
    await session.flush()
    return row


async def references(session: AsyncSession, name: str) -> list[str]:
    """Templates naming this calendar, published or draft.

    Checked before deleting because the consequence is otherwise invisible: a
    task whose calendar has gone is scheduled 24x7, deliberately and without a
    warning (`calendar_for`).
    """
    using: set[str] = set()
    for record in (await session.scalars(select(GanttTemplateRecord))).all():
        # A substring test over the serialised definition: the name can appear
        # on any flow node, and walking every shape the DSL allows to find it
        # would duplicate the parser for a check that only has to be safe.
        if name in json.dumps(record.definition or {}):
            using.add(record.name)
    for task in (await session.scalars(select(TaskTemplateRecord))).all():
        if (task.api_config or {}).get("calendar") == name:
            using.add(task.name)
    return sorted(using)


async def delete(session: AsyncSession, row: CalendarRow) -> None:
    if row.is_builtin:
        raise CalendarError(
            "E_BUILTIN_CALENDAR",
            f"`{row.name}` is built in; it can be edited but not deleted",
        )
    using = await references(session, row.name)
    if using:
        raise CalendarError(
            "E_CALENDAR_IN_USE",
            f"`{row.name}` is named by " + ", ".join(using),
        )
    await session.delete(row)
    await session.flush()
