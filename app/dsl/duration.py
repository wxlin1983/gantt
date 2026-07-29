r"""Duration format parsing (implement.md §4.5).

Pattern ``^\d+(\.\d+)?\s*[SMHD]$``, case insensitive. Compound forms such as
``1D12H`` are deliberately unsupported; write ``36H`` instead.

``D`` is 86400 seconds under ``continuous`` scheduling but means "one working
day" under ``business`` scheduling, where the length depends on the calendar.
This module therefore returns a nominal second count and leaves the calendar
aware conversion to the scheduling engine.
"""

from __future__ import annotations

import re

from .errors import DslError

_PATTERN = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([SMHDsmhd])\s*$")

_UNIT_SECONDS = {
    "S": 1,
    "M": 60,
    "H": 3600,
    "D": 86400,
}


def parse_duration(value: str | int | float, path: str = "") -> int:
    """Convert a duration such as ``12H`` into seconds.

    Bare numbers are treated as seconds so that ``duration: 0`` parses.
    """
    # bool is a subclass of int, so it has to be rejected explicitly
    if isinstance(value, bool):
        raise DslError.single(
            "E_BAD_DURATION", f"duration cannot be a boolean: {value!r}", path
        )
    if isinstance(value, int | float):
        if value < 0:
            raise DslError.single(
                "E_BAD_DURATION", f"duration cannot be negative: {value}", path
            )
        return int(value)

    if not isinstance(value, str):
        raise DslError.single(
            "E_BAD_DURATION", f"unrecognised duration: {value!r}", path
        )

    match = _PATTERN.match(value)
    if match is None:
        raise DslError.single(
            "E_BAD_DURATION",
            f"malformed duration {value!r}: expected forms like 12H, 90M, "
            "3D or 30S (compound values such as 1D12H are not supported)",
            path,
        )

    amount, unit = match.group(1), match.group(2).upper()
    return int(float(amount) * _UNIT_SECONDS[unit])


def format_duration(seconds: int) -> str:
    """Render seconds back to the most compact DSL form."""
    if seconds == 0:
        return "0S"
    for unit in ("D", "H", "M"):
        size = _UNIT_SECONDS[unit]
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}S"
