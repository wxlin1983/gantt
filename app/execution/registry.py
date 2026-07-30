"""Task handler registry (implement.md §6.1).

A handler is what drives a task through an external system. Handlers register
themselves by name; a task template names one in ``task_api``. The worker only
ever sees this interface, so a built-in HTTP handler and a hand-written Python
one are indistinguishable to it.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class Outcome(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    #: Failed in a way retrying cannot fix, so the retry budget is skipped.
    FATAL = "fatal"


@dataclass(slots=True)
class TaskResult:
    outcome: Outcome
    #: Identifier handed back by the external system, used when polling.
    external_ref: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    message: str = ""
    detail: str = ""

    @classmethod
    def running(
        cls, external_ref: str | None = None, **payload: Any
    ) -> TaskResult:
        return cls(Outcome.RUNNING, external_ref=external_ref, payload=payload)

    @classmethod
    def succeeded(cls, **payload: Any) -> TaskResult:
        return cls(Outcome.SUCCEEDED, payload=payload)

    @classmethod
    def failed(cls, message: str, detail: str = "") -> TaskResult:
        return cls(Outcome.FAILED, message=message, detail=detail)

    @classmethod
    def fatal(cls, message: str, detail: str = "") -> TaskResult:
        return cls(Outcome.FATAL, message=message, detail=detail)

    @property
    def is_terminal(self) -> bool:
        return self.outcome is not Outcome.RUNNING


@dataclass(slots=True)
class TaskContext:
    """Everything a handler is allowed to know about the task it is driving.

    Deliberately a flat snapshot rather than the ORM row: a handler runs
    arbitrary I/O, and giving it a live session would invite it to hold a
    transaction open across a network call.
    """

    case_id: int
    task_id: int
    task_name: str
    attempt: int
    params: dict[str, Any] = field(default_factory=dict)
    case_params: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    external_ref: str | None = None
    #: Resolved secret values, keyed by the name the config referenced.
    secrets: dict[str, str] = field(default_factory=dict)
    #: One-time URL the external system can post a result back to.
    callback_url: str | None = None

    @property
    def idempotency_key(self) -> str:
        """Stable within an attempt, so a retried network call is deduplicated
        by the receiving system rather than executed twice."""
        raw = f"{self.case_id}:{self.task_id}:{self.attempt}"
        return hashlib.sha256(raw.encode()).hexdigest()


@runtime_checkable
class Handler(Protocol):
    """What the worker requires of a handler."""

    async def trigger(self, ctx: TaskContext) -> TaskResult:
        """Start the work. Called once per attempt."""

    async def poll(self, ctx: TaskContext) -> TaskResult:
        """Check on work already started."""


class HandlerRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}
        self._builtin: set[str] = set()

    def register(
        self, name: str, handler: Handler, builtin: bool = False
    ) -> None:
        if name in self._handlers:
            raise ValueError(f"handler {name!r} is already registered")
        self._handlers[name] = handler
        if builtin:
            self._builtin.add(name)

    def get(self, name: str) -> Handler | None:
        return self._handlers.get(name)

    def require(self, name: str) -> Handler:
        handler = self._handlers.get(name)
        if handler is None:
            known = ", ".join(sorted(self._handlers)) or "none"
            raise LookupError(
                f"no handler named {name!r}; registered: {known}"
            )
        return handler

    def names(self) -> list[str]:
        return sorted(self._handlers)

    def describe(self) -> list[dict[str, Any]]:
        """Feeds the task template editor's dropdown (§9.8).

        Listing what actually exists is what stops a typo in ``task_api`` from
        surfacing only once a case is running.
        """
        return [
            {
                "name": name,
                "builtin": name in self._builtin,
                "description": (
                    (self._handlers[name].__doc__ or "").strip().splitlines()
                    or [""]
                )[0],
            }
            for name in sorted(self._handlers)
        ]

    def clear(self) -> None:
        """Only for tests; the process registry is otherwise write-once."""
        self._handlers.clear()
        self._builtin.clear()


registry = HandlerRegistry()


def task_handler(
    name: str, builtin: bool = False
) -> Callable[[type], type]:
    """Class decorator that registers a handler under ``name``."""

    def decorate(cls: type) -> type:
        registry.register(name, cls(), builtin=builtin)
        return cls

    return decorate


def load_builtin_handlers() -> HandlerRegistry:
    """Import the built-in handler modules for their registration side effect.

    Custom handlers live in the same package and are picked up the same way;
    importing here rather than at module scope keeps the import graph acyclic.
    """
    from app.execution import handlers  # noqa: F401

    handlers.load()
    return registry
