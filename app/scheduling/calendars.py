"""Working-time calendars (implement.md §5.1).

Two implementations behind one protocol, chosen per task so that a single case
can mix them: automated steps run 24x7 while steps needing a person only
advance during working hours.

All datetimes crossing this boundary must be timezone aware. Naive input is
rejected rather than assumed to be UTC, because a silent one-off timezone
error in a scheduling engine is close to impossible to spot afterwards.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, time, timedelta
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo

#: Guard against walking forever when a calendar has no working time at all,
#: or when a caller asks for an absurd duration.
MAX_DAYS_WALK = 3650

_WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

Segment = tuple[time, time]


class CalendarError(Exception):
    """The requested span cannot be resolved on this calendar."""


def _require_aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone aware, got {value!r}")
    return value


def _seconds_of(moment: time) -> int:
    return moment.hour * 3600 + moment.minute * 60 + moment.second


@runtime_checkable
class Calendar(Protocol):
    """Time arithmetic in working seconds."""

    name: str

    @property
    def day_seconds(self) -> int:
        """Working seconds in one day, used to convert ``D`` durations."""

    def add(self, start: datetime, seconds: int) -> datetime:
        """Advance ``seconds`` of working time from ``start``."""

    def sub(self, end: datetime, seconds: int) -> datetime:
        """Rewind ``seconds`` of working time from ``end``."""

    def next_working_instant(self, t: datetime) -> datetime:
        """The first working instant at or after ``t``."""

    def previous_working_instant(self, t: datetime) -> datetime:
        """The last working instant at or before ``t``."""

    def elapsed(self, start: datetime, end: datetime) -> int:
        """Working seconds between two instants, floored at zero."""


class ContinuousCalendar:
    """24x7 time: working seconds are just seconds."""

    __slots__ = ("name",)

    def __init__(self, name: str = "continuous"):
        self.name = name

    @property
    def day_seconds(self) -> int:
        return 86400

    def add(self, start: datetime, seconds: int) -> datetime:
        return _require_aware(start, "start") + timedelta(seconds=seconds)

    def sub(self, end: datetime, seconds: int) -> datetime:
        return _require_aware(end, "end") - timedelta(seconds=seconds)

    def next_working_instant(self, t: datetime) -> datetime:
        return _require_aware(t, "t")

    def previous_working_instant(self, t: datetime) -> datetime:
        return _require_aware(t, "t")

    def elapsed(self, start: datetime, end: datetime) -> int:
        _require_aware(start, "start")
        _require_aware(end, "end")
        return max(int((end - start).total_seconds()), 0)

    def __repr__(self) -> str:
        return f"ContinuousCalendar({self.name!r})"


class BusinessCalendar:
    """Working hours per weekday, minus holidays.

    ``add`` and ``sub`` consume whole windows at a time rather than stepping
    minute by minute, so a multi-week span costs a couple of iterations per
    day crossed instead of thousands.
    """

    __slots__ = (
        "name",
        "tz",
        "_segments",
        "_holidays",
        "_daily",
        "_day_seconds",
    )

    def __init__(
        self,
        name: str,
        working_hours: Mapping[str, Sequence[Sequence[str]]] | None = None,
        holidays: Iterable[str | date] = (),
        timezone: str = "Asia/Taipei",
        day_seconds: int | None = None,
    ):
        self.name = name
        self.tz = ZoneInfo(timezone)
        self._segments = self._parse_segments(working_hours or {})
        self._holidays = frozenset(self._parse_holidays(holidays))
        self._daily = {
            key: sum(_length(segment) for segment in segments)
            for key, segments in self._segments.items()
        }
        self._day_seconds = day_seconds or self._modal_day_seconds()

    # -- construction ------------------------------------------------------

    @staticmethod
    def _parse_segments(
        raw: Mapping[str, Sequence[Sequence[str]]],
    ) -> dict[str, list[Segment]]:
        parsed: dict[str, list[Segment]] = {}
        for key, windows in raw.items():
            day = key.strip().lower()[:3]
            if day not in _WEEKDAY_KEYS:
                raise ValueError(f"unknown weekday key: {key!r}")
            segments: list[Segment] = []
            for window in windows or ():
                if len(window) != 2:
                    raise ValueError(
                        f"working window for {key} must be [start, end], "
                        f"got {window!r}"
                    )
                begins = time.fromisoformat(str(window[0]))
                ends = time.fromisoformat(str(window[1]))
                if ends <= begins:
                    raise ValueError(
                        f"working window for {key} ends before it starts: "
                        f"{window!r}"
                    )
                segments.append((begins, ends))
            segments.sort()
            # Overlapping windows would double-count available time.
            for earlier, later in zip(segments, segments[1:], strict=False):
                if later[0] < earlier[1]:
                    raise ValueError(
                        f"working windows for {key} overlap: {segments!r}"
                    )
            parsed[day] = segments
        return parsed

    @staticmethod
    def _parse_holidays(raw: Iterable[str | date]) -> list[date]:
        return [
            entry
            if isinstance(entry, date)
            else date.fromisoformat(str(entry))
            for entry in raw
        ]

    def _modal_day_seconds(self) -> int:
        """The length of a *typical* working day.

        This is what ``1D`` means on this calendar (implement.md §4.5): a
        nine-to-six week makes ``2D`` eighteen working hours, not 172800
        working seconds -- which would be over five weeks.

        The modal length is used rather than the mean so that an irregular
        week (say a half-day Friday) still reports a normal day as a day.
        Ties break towards the longer day. Pass ``day_seconds`` explicitly to
        override.
        """
        lengths = [value for value in self._daily.values() if value > 0]
        if not lengths:
            return 0
        counts = Counter(lengths)
        best = max(counts.values())
        return max(length for length, n in counts.items() if n == best)

    # -- day queries -------------------------------------------------------

    @property
    def day_seconds(self) -> int:
        return self._day_seconds

    def segments_on(self, day: date) -> list[Segment]:
        """Working windows for a calendar day, empty on holidays."""
        if day in self._holidays:
            return []
        return self._segments.get(_WEEKDAY_KEYS[day.weekday()], [])

    def seconds_on(self, day: date) -> int:
        if day in self._holidays:
            return 0
        return self._daily.get(_WEEKDAY_KEYS[day.weekday()], 0)

    def is_working_day(self, day: date) -> bool:
        return self.seconds_on(day) > 0

    def _local(self, value: datetime) -> datetime:
        return _require_aware(value, "datetime").astimezone(self.tz)

    def _at(self, day: date, moment: time) -> datetime:
        return datetime.combine(day, moment, tzinfo=self.tz)

    def _windows(self, day: date) -> list[tuple[datetime, datetime]]:
        return [
            (self._at(day, begins), self._at(day, ends))
            for begins, ends in self.segments_on(day)
        ]

    # -- boundary snapping -------------------------------------------------

    def next_working_instant(self, t: datetime) -> datetime:
        local = self._local(t)
        for offset in range(MAX_DAYS_WALK):
            day = local.date() + timedelta(days=offset)
            for begins, ends in self._windows(day):
                # Windows are closed at both ends: finishing exactly at 18:00
                # is legitimate, and add() simply finds no time left there.
                if offset == 0 and begins <= local <= ends:
                    return t
                if begins > local:
                    return begins.astimezone(t.tzinfo)
        raise CalendarError(
            f"{self.name} has no working time within {MAX_DAYS_WALK} days "
            f"of {t.isoformat()}"
        )

    def previous_working_instant(self, t: datetime) -> datetime:
        local = self._local(t)
        for offset in range(MAX_DAYS_WALK):
            day = local.date() - timedelta(days=offset)
            for begins, ends in reversed(self._windows(day)):
                if offset == 0 and begins <= local <= ends:
                    return t
                if ends < local:
                    return ends.astimezone(t.tzinfo)
        raise CalendarError(
            f"{self.name} has no working time within {MAX_DAYS_WALK} days "
            f"before {t.isoformat()}"
        )

    # -- arithmetic --------------------------------------------------------

    def add(self, start: datetime, seconds: int) -> datetime:
        if seconds < 0:
            raise ValueError("seconds must not be negative")
        cursor = self._local(self.next_working_instant(start))
        if seconds == 0:
            return cursor.astimezone(start.tzinfo)

        remaining = seconds
        # `day` walks independently of `cursor`; deriving one from the other
        # would advance twice per iteration.
        day = cursor.date()
        for _ in range(MAX_DAYS_WALK):
            for begins, ends in self._windows(day):
                if cursor >= ends:
                    continue
                from_here = max(cursor, begins)
                available = int((ends - from_here).total_seconds())
                if remaining <= available:
                    return (
                        from_here + timedelta(seconds=remaining)
                    ).astimezone(start.tzinfo)
                remaining -= available
            # Continue from the top of the following day.
            day += timedelta(days=1)
            cursor = self._at(day, time.min)
        raise CalendarError(
            f"{seconds}s of work does not fit within {MAX_DAYS_WALK} days "
            f"of {start.isoformat()} on calendar {self.name}"
        )

    def sub(self, end: datetime, seconds: int) -> datetime:
        if seconds < 0:
            raise ValueError("seconds must not be negative")
        cursor = self._local(self.previous_working_instant(end))
        if seconds == 0:
            return cursor.astimezone(end.tzinfo)

        remaining = seconds
        day = cursor.date()
        for _ in range(MAX_DAYS_WALK):
            for begins, ends in reversed(self._windows(day)):
                if cursor <= begins:
                    continue
                until_here = min(cursor, ends)
                available = int((until_here - begins).total_seconds())
                if remaining <= available:
                    return (
                        until_here - timedelta(seconds=remaining)
                    ).astimezone(end.tzinfo)
                remaining -= available
            # Continue from the very end of the previous day, so every one of
            # its windows is still available.
            day -= timedelta(days=1)
            cursor = self._at(day, time.max)
        raise CalendarError(
            f"{seconds}s of work does not fit within {MAX_DAYS_WALK} days "
            f"before {end.isoformat()} on calendar {self.name}"
        )

    def elapsed(self, start: datetime, end: datetime) -> int:
        """Working seconds between two instants.

        Needed to work out how much of a running task is actually done: wall
        clock elapsed would charge a task 48 hours of progress for sitting
        over a weekend.
        """
        first = self._local(start)
        last = self._local(end)
        if last <= first:
            return 0

        total = 0
        day = first.date()
        for _ in range(MAX_DAYS_WALK):
            if day > last.date():
                return total
            for begins, ends in self._windows(day):
                overlap = min(ends, last) - max(begins, first)
                if overlap.total_seconds() > 0:
                    total += int(overlap.total_seconds())
            day += timedelta(days=1)
        raise CalendarError(
            f"span {start.isoformat()}..{end.isoformat()} exceeds "
            f"{MAX_DAYS_WALK} days on calendar {self.name}"
        )

    def __repr__(self) -> str:
        return f"BusinessCalendar({self.name!r}, tz={self.tz.key!r})"


def _length(segment: Segment) -> int:
    begins, ends = segment
    return _seconds_of(ends) - _seconds_of(begins)


CONTINUOUS = ContinuousCalendar()

_OFFICE_WEEK = {
    "mon": [["09:00", "18:00"]],
    "tue": [["09:00", "18:00"]],
    "wed": [["09:00", "18:00"]],
    "thu": [["09:00", "18:00"]],
    "fri": [["09:00", "18:00"]],
    "sat": [],
    "sun": [],
}


def office_calendar(
    name: str = "taiwan_office",
    holidays: Iterable[str | date] = (),
    timezone: str = "Asia/Taipei",
) -> BusinessCalendar:
    """A conventional nine-to-six week.

    Seeded as a builtin and used throughout the tests; real deployments load
    calendars from the database.
    """
    return BusinessCalendar(
        name, _OFFICE_WEEK, holidays=holidays, timezone=timezone
    )


def registry(*calendars: Calendar) -> dict[str, Calendar]:
    """Build the ``name -> calendar`` mapping the passes expect."""
    result: dict[str, Calendar] = {CONTINUOUS.name: CONTINUOUS}
    for calendar in calendars:
        result[calendar.name] = calendar
    return result
