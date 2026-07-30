"""Loading calendars out of the database and out of case snapshots.

Two sources, one shape. Live editing reads the ``calendars`` table; a running
case reads the copy frozen into its snapshot, because changing the holiday
table later must not retroactively move an existing case's dates (§4.8).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Calendar as CalendarRow
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
