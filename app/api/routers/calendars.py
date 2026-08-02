"""Working-time calendars (implement.md §3.2, §5.1).

Nothing could edit these before: no API, no CLI command, no import path. The
table shipped with `taiwan_office` holding an empty holiday list, so every
business-mode task scheduled straight through every public holiday -- the
arithmetic exact, its input empty.

Reading is open to anyone signed in, because the template editor and the case
wizard both need to name the available calendars. Writing is administrator
only.

**A change here only affects cases created afterwards.** Every case freezes the
calendar definitions into its snapshot at creation and reschedules against that
copy (§4.8), which is the whole point of snapshot isolation -- adding a holiday
must not silently move dates somebody has already committed to.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from app.auth import permissions
from app.services import calendars as calendar_service

from ..deps import PrincipalDep, SessionDep, require
from ..errors import ApiError
from ..schemas import (
    CalendarOut,
    CreateCalendarRequest,
    UpdateCalendarRequest,
)

router = APIRouter(tags=["calendars"])


def _out(row) -> CalendarOut:
    return CalendarOut(
        id=row.id,
        name=row.name,
        timezone=row.timezone,
        working_hours=row.working_hours or {},
        holidays=list(row.holidays or []),
        is_builtin=row.is_builtin,
        day_seconds=calendar_service.day_seconds_of(row),
        is_editable=row.name != calendar_service.BUILTIN_CONTINUOUS,
    )


@router.get("/calendars", response_model=list[CalendarOut])
async def list_calendars(
    session: SessionDep, principal: PrincipalDep
) -> list[CalendarOut]:
    require(permissions.can_view(principal), "sign in first")
    return [_out(row) for row in await calendar_service.list_rows(session)]


@router.post(
    "/calendars",
    response_model=CalendarOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_calendar(
    body: CreateCalendarRequest, session: SessionDep, principal: PrincipalDep
) -> CalendarOut:
    require(
        permissions.can_manage_calendars(principal),
        "only an administrator can add calendars",
    )
    try:
        row = await calendar_service.create(
            session,
            name=body.name,
            timezone=body.timezone,
            working_hours=body.working_hours,
            holidays=body.holidays,
        )
    except calendar_service.CalendarError as exc:
        raise ApiError(exc.code, str(exc)) from exc
    return _out(row)


@router.patch("/calendars/{calendar_id}", response_model=CalendarOut)
async def update_calendar(
    calendar_id: int,
    body: UpdateCalendarRequest,
    session: SessionDep,
    principal: PrincipalDep,
) -> CalendarOut:
    require(
        permissions.can_manage_calendars(principal),
        "only an administrator can edit calendars",
    )
    try:
        row = await calendar_service.get(session, calendar_id)
        row = await calendar_service.update(
            session,
            row,
            timezone=body.timezone,
            working_hours=body.working_hours,
            holidays=body.holidays,
        )
    except calendar_service.CalendarError as exc:
        raise ApiError(exc.code, str(exc)) from exc
    return _out(row)


@router.delete(
    "/calendars/{calendar_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_calendar(
    calendar_id: int, session: SessionDep, principal: PrincipalDep
) -> None:
    require(
        permissions.can_manage_calendars(principal),
        "only an administrator can delete calendars",
    )
    try:
        row = await calendar_service.get(session, calendar_id)
        await calendar_service.delete(session, row)
    except calendar_service.CalendarError as exc:
        raise ApiError(exc.code, str(exc)) from exc
