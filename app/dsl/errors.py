"""Structured validation errors and warnings (implement.md §4.7)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class Issue:
    """A single validation result.

    `path` points at the location in the DSL (e.g.
    ``flow[2].requirement``) so the editor can jump to it.
    """

    code: str
    message: str
    severity: Severity
    path: str = ""
    details: dict = field(default_factory=dict)

    def __str__(self) -> str:
        where = f" @ {self.path}" if self.path else ""
        return f"[{self.code}]{where} {self.message}"


def error(code: str, message: str, path: str = "", **details) -> Issue:
    return Issue(code, message, Severity.ERROR, path, details)


def warning(code: str, message: str, path: str = "", **details) -> Issue:
    return Issue(code, message, Severity.WARNING, path, details)


class DslError(Exception):
    """Parsing or expansion failed.

    Carries every issue rather than only the first, so the editor can show
    a complete problem list in one pass.
    """

    def __init__(self, issues: list[Issue]):
        self.issues = issues
        super().__init__("; ".join(str(i) for i in issues) or "DSL error")

    @classmethod
    def single(
        cls, code: str, message: str, path: str = "", **details
    ) -> DslError:
        return cls([error(code, message, path, **details)])
