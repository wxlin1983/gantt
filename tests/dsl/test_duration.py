"""Duration format parsing (implement.md §4.5)."""

from __future__ import annotations

import pytest

from app.dsl.duration import format_duration, parse_duration
from app.dsl.errors import DslError


class TestParsing:
    @pytest.mark.parametrize(
        "written,seconds",
        [
            ("30S", 30),
            ("90M", 5400),
            ("12H", 43200),
            ("3D", 259200),
            ("12h", 43200),
            ("  12 H ", 43200),
            ("1.5H", 5400),
            (0, 0),
            (120, 120),
        ],
    )
    def test_accepted_forms(self, written, seconds):
        assert parse_duration(written) == seconds

    @pytest.mark.parametrize(
        "written",
        [
            "1D12H",  # compound forms are deliberately unsupported
            "12",
            "12X",
            "H12",
            "",
            "abc",
            -1,
            True,
            None,
            [],
        ],
    )
    def test_rejected_forms(self, written):
        with pytest.raises(DslError) as exc:
            parse_duration(written)
        assert exc.value.issues[0].code == "E_BAD_DURATION"

    def test_bool_is_not_an_integer_duration(self):
        # bool subclasses int, so `duration: true` would otherwise mean 1s
        with pytest.raises(DslError):
            parse_duration(True)


class TestFormatting:
    @pytest.mark.parametrize(
        "seconds,written",
        [
            (0, "0S"),
            (30, "30S"),
            (5400, "90M"),
            (43200, "12H"),
            (259200, "3D"),
            (90, "90S"),
        ],
    )
    def test_round_trip(self, seconds, written):
        assert format_duration(seconds) == written
        assert parse_duration(written) == seconds
