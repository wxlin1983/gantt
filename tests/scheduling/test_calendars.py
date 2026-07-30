"""Calendar arithmetic (implement.md §5.1).

The business calendar is checked against a deliberately naive reference that
steps a minute at a time. Hand-written expectations are easy to get wrong --
several of the values here were wrong on the first attempt -- so the property
tests are the real safety net and the explicit cases document intent.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from app.scheduling import CONTINUOUS, BusinessCalendar, office_calendar
from app.scheduling.calendars import CalendarError

from .conftest import at, hhmm

STEP = 60


def _is_working(calendar, moment):
    """Half-open: the instant a window closes is no longer working time."""
    local = moment.astimezone(calendar.tz)
    return any(
        calendar._at(local.date(), begins)
        <= local
        < calendar._at(local.date(), ends)
        for begins, ends in calendar.segments_on(local.date())
    )


def forward_reference(calendar, start, seconds):
    """Advance one minute at a time, counting only working minutes."""
    remaining, cursor = seconds, start
    while remaining > 0:
        if _is_working(calendar, cursor):
            remaining -= STEP
        cursor += timedelta(seconds=STEP)
    return cursor


def backward_reference(calendar, end, seconds):
    """Retreat one minute at a time. Written independently of the above."""
    remaining, cursor = seconds, end
    while remaining > 0:
        cursor -= timedelta(seconds=STEP)
        if _is_working(calendar, cursor):
            remaining -= STEP
    return cursor


def elapsed_reference(calendar, start, end):
    total, cursor = 0, start
    while cursor < end:
        if _is_working(calendar, cursor):
            total += STEP
        cursor += timedelta(seconds=STEP)
    return total


#: Kept under its old name for the explicit cases that still call it.
working_seconds_reference = forward_reference


class TestContinuous:
    def test_add_and_sub_are_plain_arithmetic(self):
        start = at("2026-08-14 06:00")
        assert hhmm(CONTINUOUS.add(start, 12 * 3600)) == "2026-08-14 18:00"
        assert hhmm(CONTINUOUS.sub(start, 6 * 3600)) == "2026-08-14 00:00"

    def test_every_instant_is_a_working_instant(self):
        midnight = at("2026-08-16 03:00")
        assert CONTINUOUS.next_working_instant(midnight) == midnight
        assert CONTINUOUS.previous_working_instant(midnight) == midnight

    def test_a_day_is_24_hours(self):
        assert CONTINUOUS.day_seconds == 86400

    def test_elapsed_is_wall_clock(self):
        span = CONTINUOUS.elapsed(
            at("2026-08-14 16:00"), at("2026-08-17 11:00")
        )
        assert span == 67 * 3600

    def test_naive_datetime_is_rejected(self):
        # A silent timezone assumption in a scheduler is undebuggable later.
        with pytest.raises(ValueError):
            CONTINUOUS.add(datetime(2026, 8, 14, 6, 0), 60)


class TestBusinessDayLength:
    def test_nine_to_six_is_nine_hours(self, office):
        assert office.day_seconds == 9 * 3600

    def test_irregular_week_reports_the_typical_day(self):
        # Half-day Friday must not drag "one day" below a normal day.
        calendar = BusinessCalendar(
            "mixed",
            {
                "mon": [["09:00", "18:00"]],
                "tue": [["09:00", "18:00"]],
                "wed": [["09:00", "18:00"]],
                "thu": [["09:00", "18:00"]],
                "fri": [["09:00", "13:00"]],
            },
        )
        assert calendar.day_seconds == 9 * 3600

    def test_explicit_override(self):
        calendar = BusinessCalendar(
            "custom", {"mon": [["09:00", "18:00"]]}, day_seconds=8 * 3600
        )
        assert calendar.day_seconds == 8 * 3600

    def test_split_shift_sums_both_windows(self):
        calendar = BusinessCalendar(
            "split", {"mon": [["09:00", "12:00"], ["13:00", "18:00"]]}
        )
        assert calendar.day_seconds == 8 * 3600


class TestBusinessArithmetic:
    @pytest.mark.parametrize(
        "start,hours,expected",
        [
            # Within one day
            ("2026-08-14 09:00", 9, "2026-08-14 18:00"),
            ("2026-08-14 10:00", 1, "2026-08-14 11:00"),
            # Spilling over the weekend
            ("2026-08-14 16:00", 4, "2026-08-17 11:00"),
            ("2026-08-14 09:00", 10, "2026-08-17 10:00"),
            # Starting outside working hours snaps forward first
            ("2026-08-15 12:00", 1, "2026-08-17 10:00"),
            ("2026-08-14 20:00", 1, "2026-08-17 10:00"),
            # Exactly five working days
            ("2026-08-14 09:00", 45, "2026-08-20 18:00"),
            ("2026-08-14 09:00", 46, "2026-08-21 10:00"),
        ],
    )
    def test_add(self, office, start, hours, expected):
        assert hhmm(office.add(at(start), hours * 3600)) == expected

    @pytest.mark.parametrize(
        "end,hours,expected",
        [
            ("2026-08-17 11:00", 4, "2026-08-14 16:00"),
            ("2026-08-17 09:00", 1, "2026-08-14 17:00"),
            ("2026-08-16 12:00", 1, "2026-08-14 17:00"),
            ("2026-08-17 09:00", 20, "2026-08-12 16:00"),
            ("2026-08-24 18:00", 45, "2026-08-18 09:00"),
        ],
    )
    def test_sub(self, office, end, hours, expected):
        assert hhmm(office.sub(at(end), hours * 3600)) == expected

    def test_zero_duration_snaps_to_a_working_instant(self, office):
        assert (
            hhmm(office.add(at("2026-08-15 12:00"), 0)) == "2026-08-17 09:00"
        )
        assert (
            hhmm(office.sub(at("2026-08-15 12:00"), 0)) == "2026-08-14 18:00"
        )

    def test_negative_duration_is_rejected(self, office):
        with pytest.raises(ValueError):
            office.add(at("2026-08-14 09:00"), -60)


class TestHolidays:
    def test_holiday_is_skipped(self):
        calendar = office_calendar(holidays=["2026-08-17"])
        # Friday 16:00 + 4h: 2h Friday, then Monday is a holiday
        assert (
            hhmm(calendar.add(at("2026-08-14 16:00"), 4 * 3600))
            == "2026-08-18 11:00"
        )

    def test_holiday_has_no_working_seconds(self):
        calendar = office_calendar(holidays=["2026-08-17"])
        assert not calendar.is_working_day(at("2026-08-17").date())
        assert calendar.segments_on(at("2026-08-17").date()) == []


class TestSnapping:
    def test_inside_a_window_is_unchanged(self, office):
        moment = at("2026-08-14 10:00")
        assert office.next_working_instant(moment) == moment
        assert office.previous_working_instant(moment) == moment

    def test_window_boundaries_count_as_working(self, office):
        for boundary in ("2026-08-14 09:00", "2026-08-14 18:00"):
            moment = at(boundary)
            assert office.next_working_instant(moment) == moment
            assert office.previous_working_instant(moment) == moment

    def test_weekend_snaps_both_ways(self, office):
        saturday = at("2026-08-15 12:00")
        assert (
            hhmm(office.next_working_instant(saturday)) == "2026-08-17 09:00"
        )
        assert (
            hhmm(office.previous_working_instant(saturday))
            == "2026-08-14 18:00"
        )

    def test_lunch_break_snaps_to_the_afternoon(self):
        calendar = BusinessCalendar(
            "split",
            {"fri": [["09:00", "12:00"], ["13:00", "18:00"]]},
        )
        noon = at("2026-08-14 12:30")
        assert hhmm(calendar.next_working_instant(noon)) == "2026-08-14 13:00"
        assert (
            hhmm(calendar.previous_working_instant(noon)) == "2026-08-14 12:00"
        )


class TestElapsed:
    def test_counts_only_working_time(self, office):
        # Without this, a task sitting over a weekend would be credited with
        # 48 hours of progress it never made.
        span = office.elapsed(at("2026-08-14 16:00"), at("2026-08-17 11:00"))
        assert span == 4 * 3600

    def test_span_entirely_outside_working_hours_is_zero(self, office):
        assert (
            office.elapsed(at("2026-08-14 18:00"), at("2026-08-17 09:00")) == 0
        )

    def test_reversed_span_is_zero(self, office):
        assert (
            office.elapsed(at("2026-08-17 11:00"), at("2026-08-14 16:00")) == 0
        )


class TestAgainstReference:
    """Property tests: the implementation must match a naive stepper."""

    def test_add_matches_minute_by_minute_reference(self):
        calendar = office_calendar(holidays=["2026-08-17", "2026-09-28"])
        random.seed(7)
        for _ in range(120):
            base = at("2026-08-01 00:00") + timedelta(
                minutes=random.randrange(0, 60 * 24 * 60)
            )
            seconds = random.randrange(1, 40) * 3600
            expected = working_seconds_reference(
                calendar, calendar.next_working_instant(base), seconds
            )
            assert calendar.add(base, seconds) == expected

    def test_sub_inverts_add(self):
        calendar = office_calendar(holidays=["2026-08-17"])
        random.seed(11)
        for _ in range(120):
            base = at("2026-08-01 00:00") + timedelta(
                minutes=random.randrange(0, 60 * 24 * 60)
            )
            seconds = random.randrange(1, 40) * 3600
            start = calendar.next_working_instant(base)
            assert calendar.sub(calendar.add(start, seconds), seconds) == start

    def test_sub_matches_its_own_backward_reference(self):
        """Not just the inverse of add.

        A sign error applied symmetrically to both would satisfy the inverse
        property while getting every absolute answer wrong, so `sub` is also
        checked against a stepper that knows nothing about `add`.
        """
        calendar = office_calendar(holidays=["2026-08-17"])
        random.seed(13)
        for _ in range(120):
            base = at("2026-08-01 00:00") + timedelta(
                minutes=random.randrange(0, 60 * 24 * 60)
            )
            seconds = random.randrange(1, 400) * 60
            end = calendar.previous_working_instant(base)
            assert calendar.sub(base, seconds) == backward_reference(
                calendar, end, seconds
            )

    def test_elapsed_matches_a_minute_counting_reference(self):
        calendar = office_calendar(holidays=["2026-08-17"])
        random.seed(17)
        for _ in range(80):
            start = at("2026-08-01 00:00") + timedelta(
                minutes=random.randrange(0, 60 * 24 * 40)
            )
            end = start + timedelta(minutes=random.randrange(1, 60 * 24 * 8))
            assert calendar.elapsed(start, end) == elapsed_reference(
                calendar, start, end
            )


class TestDaylightSaving:
    """Zones that shift, which the arithmetic must measure in real time.

    Python subtracts two aware datetimes sharing a ``tzinfo`` naively -- the
    documented behaviour is that the common offset is ignored -- and every
    window boundary is built from one ``ZoneInfo``. Left in local time the
    calendar counted wall-clock hours, so a nine-hour day reported nine hours
    on the morning its zone sprang forward and only eight had passed.
    """

    @staticmethod
    def night_shift(timezone: str) -> BusinessCalendar:
        """Midnight to six, so the window contains the transition itself."""
        return BusinessCalendar(
            "nights",
            {
                day: [["00:00", "06:00"]]
                for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
            },
            timezone=timezone,
        )

    def test_spring_forward_day_is_an_hour_shorter(self):
        # New York moves 02:00 -> 03:00 on 2026-03-08, so the 00:00-06:00
        # window holds five hours of real time, not six
        calendar = self.night_shift("America/New_York")
        midnight = datetime(2026, 3, 8, 5, tzinfo=UTC)  # 00:00 EST
        assert calendar.elapsed(midnight, midnight + timedelta(hours=6)) == (
            5 * 3600
        )

    def test_autumn_back_day_is_an_hour_longer(self):
        # And 01:00 -> 00:00 on 2026-11-01 gives that window seven
        calendar = self.night_shift("America/New_York")
        midnight = datetime(2026, 11, 1, 4, tzinfo=UTC)  # 00:00 EDT
        assert calendar.elapsed(midnight, midnight + timedelta(hours=8)) == (
            7 * 3600
        )

    def test_add_crosses_a_transition_in_real_time(self):
        calendar = self.night_shift("America/New_York")
        start = datetime(2026, 3, 8, 5, tzinfo=UTC)
        # Five hours is all that day has left, so the sixth lands next day
        assert calendar.add(start, 6 * 3600) == datetime(
            2026, 3, 9, 5, tzinfo=UTC
        )

    def test_sub_crosses_a_transition_in_real_time(self):
        calendar = self.night_shift("America/New_York")
        end = datetime(2026, 3, 8, 10, tzinfo=UTC)  # 06:00 EDT
        assert calendar.sub(end, 6 * 3600) == datetime(
            2026, 3, 7, 10, tzinfo=UTC
        )

    @pytest.mark.parametrize(
        "timezone",
        [
            "America/New_York",
            "Europe/London",
            "Australia/Sydney",
            # A 30-minute shift on a half-hour base offset
            "Australia/Lord_Howe",
        ],
    )
    def test_add_matches_the_reference_across_transitions(self, timezone):
        calendar = self.night_shift(timezone)
        random.seed(19)
        for base in (
            datetime(2026, 3, 1, tzinfo=UTC),
            datetime(2026, 10, 22, tzinfo=UTC),
        ):
            for _ in range(20):
                start = base + timedelta(
                    minutes=random.randrange(0, 60 * 24 * 14)
                )
                seconds = random.randrange(1, 400) * 60
                snapped = calendar.next_working_instant(start)
                assert calendar.add(start, seconds) == forward_reference(
                    calendar, snapped, seconds
                )
                # Consuming n working seconds must measure back as n
                assert (
                    calendar.elapsed(snapped, calendar.add(start, seconds))
                    == seconds
                )


class TestDegenerateCalendars:
    def test_calendar_with_no_working_time_raises(self):
        empty = BusinessCalendar("empty", {"mon": []})
        with pytest.raises(CalendarError):
            empty.add(at("2026-08-14 09:00"), 3600)

    def test_overlapping_windows_are_rejected(self):
        with pytest.raises(ValueError, match="overlap"):
            BusinessCalendar(
                "bad", {"mon": [["09:00", "13:00"], ["12:00", "18:00"]]}
            )

    def test_backwards_window_is_rejected(self):
        with pytest.raises(ValueError, match="ends before"):
            BusinessCalendar("bad", {"mon": [["18:00", "09:00"]]})

    def test_unknown_weekday_is_rejected(self):
        with pytest.raises(ValueError, match="weekday"):
            BusinessCalendar("bad", {"funday": [["09:00", "18:00"]]})
