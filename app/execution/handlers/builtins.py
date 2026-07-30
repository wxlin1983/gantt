"""The remaining built-in handlers (implement.md §6.1.1)."""

from __future__ import annotations

import asyncio
import shlex

from app.config import get_settings
from app.execution.registry import TaskContext, TaskResult, task_handler


@task_handler("wait_for_signal", builtin=True)
class WaitForSignalHandler:
    """Do nothing and wait for an external system to post a result back."""

    async def trigger(self, ctx: TaskContext) -> TaskResult:
        # The callback URL is minted before the handler runs, so there is
        # nothing to do here but declare the task in flight.
        return TaskResult.running(external_ref=ctx.callback_url)

    async def poll(self, ctx: TaskContext) -> TaskResult:
        # Polling never resolves this handler; only the callback does. The
        # timeout check is what eventually gives up.
        return TaskResult.running(external_ref=ctx.external_ref)


@task_handler("shell_command", builtin=True)
class ShellCommandHandler:
    """Run an allowlisted local command.

    Disabled unless explicitly switched on, and then restricted to named
    commands. A template-driven shell handler is remote code execution by
    another route, so the default has to be off rather than open.
    """

    timeout_seconds = 300.0

    async def trigger(self, ctx: TaskContext) -> TaskResult:
        settings = get_settings()
        if not settings.shell_handler_enabled:
            return TaskResult.fatal(
                "shell_command is disabled; set SHELL_HANDLER_ENABLED=true "
                "and populate SHELL_HANDLER_ALLOWED_COMMANDS to use it"
            )

        raw = str(ctx.config.get("command", "")).strip()
        if not raw:
            return TaskResult.fatal("api_config.command is not set")

        # Split first, then check the program name: passing the whole string
        # to a shell would make the allowlist meaningless.
        argv = shlex.split(raw)
        argv += [
            shlex.quote(str(arg)) for arg in ctx.config.get("args") or []
        ]
        program = argv[0]
        if program not in settings.shell_handler_allowed_commands:
            return TaskResult.fatal(
                f"{program!r} is not in SHELL_HANDLER_ALLOWED_COMMANDS"
            )

        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.timeout_seconds
            )
        except TimeoutError:
            return TaskResult.failed(f"{program} timed out")
        except OSError as exc:
            return TaskResult.fatal(f"{program} could not be run: {exc}")

        if process.returncode != 0:
            return TaskResult.failed(
                f"{program} exited {process.returncode}",
                detail=stderr.decode(errors="replace")[:2000],
            )
        return TaskResult.succeeded(
            stdout=stdout.decode(errors="replace")[:2000]
        )

    async def poll(self, ctx: TaskContext) -> TaskResult:
        # trigger runs to completion, so a poll can only mean the worker died
        # mid-run; treating that as failure is safer than assuming success.
        return TaskResult.failed(
            "shell_command has no state to poll; the run was interrupted"
        )
