"""Command line entry points.

The phase 1 deliverable: point it at a template plus a set of parameters and
it prints the task graph that a case would be built from, without touching a
database. That makes it the fastest way to check whether the DSL can express
a real flow.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo

import typer
import yaml
from rich.console import Console
from rich.table import Table

from app.config import get_settings
from app.dsl.duration import format_duration
from app.dsl.errors import DslError, Issue
from app.dsl.expansion import expand
from app.dsl.loader import parse_gantt_template, parse_task_template
from app.dsl.schema import ExpansionResult, GanttTemplate, TaskTemplate
from app.scheduling import (
    apply_baseline,
    backward_pass,
    evaluate,
    from_expansion,
    office_calendar,
    registry,
)

app = typer.Typer(help="Template-driven gantt system", no_args_is_help=True)
# A fixed width when piped keeps the tables readable in logs and CI.
_WIDTH = None if sys.stdout.isatty() else 120
console = Console(width=_WIDTH)
errors = Console(stderr=True, width=_WIDTH)


def _load_task_templates(paths: list[Path]) -> dict[str, TaskTemplate]:
    """Load task templates from files or directories of YAML."""
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(path.glob("*.yaml")))
            files.extend(sorted(path.glob("*.yml")))
        else:
            files.append(path)

    templates: dict[str, TaskTemplate] = {}
    issues: list[Issue] = []
    for file in files:
        text = file.read_text()
        # One file may hold several task templates as a YAML stream
        for document in yaml.safe_load_all(text):
            if document is None:
                continue
            try:
                template = parse_task_template(document)
            except DslError as exc:
                issues.extend(
                    Issue(
                        i.code,
                        i.message,
                        i.severity,
                        f"{file.name}:{i.path}",
                        i.details,
                    )
                    for i in exc.issues
                )
                continue
            templates[template.id] = template
    if issues:
        raise DslError(issues)
    return templates


def _parse_assignments(pairs: list[str], label: str) -> dict[str, Any]:
    """Turn ``--param n=3`` style options into a mapping."""
    result: dict[str, Any] = {}
    for pair in pairs:
        key, separator, value = pair.partition("=")
        if not separator:
            raise typer.BadParameter(
                f"{label} must be written as name=value (got {pair!r})"
            )
        result[key.strip()] = value
    return result


def _report(issues: list[Issue], header: str) -> None:
    errors.print(f"[bold red]{header}[/]")
    for issue in issues:
        location = f" [dim]{issue.path}[/]" if issue.path else ""
        errors.print(f"  [red]{issue.code}[/]{location}  {issue.message}")


def _print_warnings(warnings: list[Issue]) -> None:
    if not warnings:
        return
    console.print()
    for issue in warnings:
        location = f" [dim]{issue.path}[/]" if issue.path else ""
        console.print(f"  [yellow]{issue.code}[/]{location}  {issue.message}")


def _print_result(template: GanttTemplate, result: ExpansionResult) -> None:
    tasks = Table(
        title=f"{template.template_name} v{template.version} — "
        f"{len(result.tasks)} tasks",
        title_justify="left",
        header_style="bold",
    )
    for column in ("id", "label", "phase", "owner", "duration", "flags"):
        tasks.add_column(column)

    for task in result.tasks:
        flags = []
        if task.optional:
            flags.append("optional")
        if task.task_api:
            flags.append(f"api:{task.task_api}")
        owner = task.owner or f"[dim]<{task.owner_source}>[/]"
        tasks.add_row(
            task.id,
            task.label,
            task.phase or "[dim]-[/]",
            owner,
            format_duration(task.duration_seconds),
            ", ".join(flags),
        )
    console.print(tasks)

    if result.edges:
        edges = Table(
            title=f"{len(result.edges)} dependencies",
            title_justify="left",
            header_style="bold",
        )
        edges.add_column("predecessor")
        edges.add_column("successor")
        edges.add_column("lag")
        for edge in result.edges:
            lag = (
                format_duration(edge.lag_seconds)
                if edge.lag_seconds
                else "[dim]-[/]"
            )
            edges.add_row(edge.predecessor, edge.successor, lag)
        console.print()
        console.print(edges)

    if result.skipped:
        skipped = Table(
            title=f"{len(result.skipped)} skipped",
            title_justify="left",
            header_style="bold",
        )
        skipped.add_column("id")
        skipped.add_column("label")
        skipped.add_column("reason")
        for entry in result.skipped:
            skipped.add_row(entry.id, entry.label, entry.reason)
        console.print()
        console.print(skipped)

    if result.buffer_seconds:
        console.print(
            f"\nProject buffer: "
            f"[bold]{format_duration(result.buffer_seconds)}[/]"
        )


@app.command("expand")
def expand_template(
    template_path: Annotated[
        Path,
        typer.Argument(
            exists=True, dir_okay=False, help="Gantt template YAML"
        ),
    ],
    tasks: Annotated[
        list[Path],
        typer.Option(
            "--tasks",
            "-t",
            exists=True,
            help="Task template file or directory (repeatable)",
        ),
    ] = [],
    param: Annotated[
        list[str],
        typer.Option("--param", "-p", help="Parameter as name=value"),
    ] = [],
    role: Annotated[
        list[str],
        typer.Option("--role", "-r", help="Role binding as role=username"),
    ] = [],
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit JSON instead of tables")
    ] = False,
) -> None:
    """Expand a template with parameters and print the resulting task graph."""
    try:
        template = parse_gantt_template(template_path.read_text())
        task_templates = _load_task_templates(tasks)
        result = expand(
            template,
            task_templates,
            params=_parse_assignments(param, "--param"),
            roles=_parse_assignments(role, "--role"),
            case={"name": template_path.stem},
        )
    except DslError as exc:
        _report(exc.issues, "Expansion failed")
        raise typer.Exit(1) from exc

    if as_json:
        console.print_json(result.model_dump_json())
        return
    _print_result(template, result)
    _print_warnings(result.warnings)


@app.command("schedule")
def schedule_command(
    template_path: Annotated[
        Path,
        typer.Argument(
            exists=True, dir_okay=False, help="Gantt template YAML"
        ),
    ],
    target: Annotated[
        str,
        typer.Option(
            "--target",
            "-T",
            help="Target completion, e.g. 2026-08-21T18:00",
        ),
    ],
    tasks: Annotated[
        list[Path],
        typer.Option(
            "--tasks",
            "-t",
            exists=True,
            help="Task template file or directory (repeatable)",
        ),
    ] = [],
    param: Annotated[
        list[str],
        typer.Option("--param", "-p", help="Parameter as name=value"),
    ] = [],
    role: Annotated[
        list[str],
        typer.Option("--role", "-r", help="Role binding as role=username"),
    ] = [],
) -> None:
    """Expand a template and work the schedule back from a target date.

    Runs the same backward and forward passes case creation would, without a
    database, so a template's real dates can be checked before committing to
    it.
    """
    zone = ZoneInfo(get_settings().default_timezone)
    try:
        target_date = datetime.fromisoformat(target)
    except ValueError as exc:
        raise typer.BadParameter(
            f"--target must be an ISO datetime, got {target!r}"
        ) from exc
    if target_date.tzinfo is None:
        target_date = target_date.replace(tzinfo=zone)

    try:
        template = parse_gantt_template(template_path.read_text())
        result = expand(
            template,
            _load_task_templates(tasks),
            params=_parse_assignments(param, "--param"),
            roles=_parse_assignments(role, "--role"),
            case={"name": template_path.stem},
        )
    except DslError as exc:
        _report(exc.issues, "Expansion failed")
        raise typer.Exit(1) from exc

    schedule_tasks, edges = from_expansion(result)
    calendars = registry(office_calendar())
    baseline = backward_pass(
        schedule_tasks,
        edges,
        target_date,
        buffer_seconds=result.buffer_seconds,
        calendars=calendars,
    )
    apply_baseline(schedule_tasks, baseline.intervals)
    outlook = evaluate(
        schedule_tasks,
        edges,
        target_date,
        datetime.now(tz=zone),
        buffer_seconds=result.buffer_seconds,
        calendars=calendars,
        forecast=None,
    )

    labels = {task.id: task.label for task in result.tasks}
    table = Table(
        title=(
            f"{template.template_name} — baseline to "
            f"{target_date:%Y-%m-%d %H:%M}"
        ),
        title_justify="left",
        header_style="bold",
    )
    for column in ("task", "start", "end", "duration", "cal", ""):
        table.add_column(column)
    for entry in sorted(result.tasks, key=lambda t: baseline.start_of(t.id)):
        interval = baseline[entry.id]
        table.add_row(
            labels.get(entry.id, entry.id),
            f"{interval.start.astimezone(zone):%m-%d %H:%M}",
            f"{interval.end.astimezone(zone):%m-%d %H:%M}",
            format_duration(entry.duration_seconds),
            entry.calendar,
            "critical" if entry.id in outlook.critical_path else "",
        )
    console.print(table)

    span = target_date - baseline.earliest_start
    begins = baseline.earliest_start.astimezone(zone)
    console.print(
        f"\nEarliest start [bold]{begins:%Y-%m-%d %H:%M}[/]"
        f"  ·  span [bold]{format_duration(int(span.total_seconds()))}[/]"
        f"  ·  critical path [bold]{len(outlook.critical_path)}[/] of "
        f"{len(result.tasks)} tasks"
    )
    if result.buffer_seconds:
        plan_ends = outlook.plan_deadline.astimezone(zone)
        console.print(
            f"Project buffer [bold]{format_duration(result.buffer_seconds)}[/]"
            f"  ·  plan ends {plan_ends:%m-%d %H:%M}"
        )
    _print_warnings(result.warnings)


@app.command("validate")
def validate(
    template_path: Annotated[
        Path,
        typer.Argument(
            exists=True, dir_okay=False, help="Gantt template YAML"
        ),
    ],
    tasks: Annotated[
        list[Path],
        typer.Option(
            "--tasks",
            "-t",
            exists=True,
            help="Task template file or directory (repeatable)",
        ),
    ] = [],
) -> None:
    """Check a template's structure without supplying parameters.

    Only syntax and references can be checked here. A template using `when`
    changes shape with its parameters, so a clean result does not prove the
    resulting graph is sound (implement.md §4.7). Use `expand` with a real
    parameter set for that.
    """
    try:
        template = parse_gantt_template(template_path.read_text())
        _load_task_templates(tasks)
    except DslError as exc:
        _report(exc.issues, "Validation failed")
        raise typer.Exit(1) from exc

    console.print(
        f"[green]OK[/] {template.template_name} v{template.version}: "
        f"{len(template.flow)} nodes, "
        f"{len(template.template_para)} parameters, "
        f"{len(template.roles)} roles"
    )
    dynamic = [node.id for node in template.flow if node.when is not None]
    if dynamic:
        console.print(
            "[yellow]note[/] this template's shape depends on parameters "
            f"({', '.join(dynamic)}); run `expand` with a real parameter set "
            "to check the result"
        )


@app.command("import")
def import_command(
    template_path: Annotated[
        Path,
        typer.Argument(
            exists=True, dir_okay=False, help="Gantt template YAML"
        ),
    ],
    tasks: Annotated[
        list[Path],
        typer.Option(
            "--tasks",
            "-t",
            exists=True,
            help="Task template file or directory (repeatable)",
        ),
    ] = [],
    publish: Annotated[
        bool,
        typer.Option(
            "--publish", help="Publish immediately instead of leaving a draft"
        ),
    ] = False,
) -> None:
    """Load a template into the database, creating any task templates it needs.

    Imports land as a draft by default so they can be reviewed; `--publish`
    is the shortcut for setting up a fresh installation.
    """
    import asyncio

    import yaml as yaml_module

    from app.db import session_scope
    from app.services import templates as template_service

    gantt = yaml_module.safe_load(template_path.read_text())
    document = {"gantt": gantt.get("gantt", gantt)}

    task_documents: list[dict] = []
    for path in tasks:
        files = (
            sorted(path.glob("*.yaml")) + sorted(path.glob("*.yml"))
            if path.is_dir()
            else [path]
        )
        for file in files:
            for entry in yaml_module.safe_load_all(file.read_text()):
                if entry:
                    task_documents.append(entry.get("task", entry))
    if task_documents:
        document["task_templates"] = task_documents

    async def run() -> str:
        async with session_scope() as session:
            report = await template_service.import_document(
                session, yaml_module.safe_dump(document)
            )
            lines = [
                f"imported {report.template_name} as draft "
                f"v{report.draft_version}"
            ]
            if report.task_templates_created:
                lines.append(
                    "created task templates: "
                    + ", ".join(report.task_templates_created)
                )
            if report.task_templates_differing:
                lines.append(
                    "left existing task templates alone (they differ): "
                    + ", ".join(report.task_templates_differing)
                )
            if report.missing_credentials:
                lines.append(
                    "credentials still to configure: "
                    + ", ".join(report.missing_credentials)
                )
            if publish:
                published = await template_service.publish(
                    session, report.template_name, "imported via CLI"
                )
                lines.append(f"published v{published.version}")
            return "\n  ".join(lines)

    try:
        console.print(f"[green]OK[/] {asyncio.run(run())}")
    except DslError as exc:
        _report(exc.issues, "Import failed")
        raise typer.Exit(1) from exc


@app.command("seed")
def seed_command(
    admin_username: Annotated[
        str, typer.Option("--admin", help="Username for the first admin")
    ] = "admin",
    admin_email: Annotated[str, typer.Option("--email")] = "",
    password: Annotated[
        str,
        typer.Option(
            "--password",
            prompt=True,
            hide_input=True,
            confirmation_prompt=True,
            help="Password for the first admin",
        ),
    ] = "",
) -> None:
    """Prepare an empty database: builtin calendars and the first admin.

    Idempotent, so it is safe to run against a database that has already been
    seeded -- existing rows are left alone.
    """
    import asyncio

    from app.auth.passwords import hash_password
    from app.db import session_scope
    from app.models import User
    from app.services import calendars as calendar_service

    async def run() -> str:
        from sqlalchemy import select

        async with session_scope() as session:
            await calendar_service.ensure_builtins(session)
            existing = (
                await session.scalars(
                    select(User).where(User.username == admin_username)
                )
            ).first()
            if existing is not None:
                return f"admin {admin_username!r} already exists"
            session.add(
                User(
                    username=admin_username,
                    display_name=admin_username.title(),
                    email=admin_email or f"{admin_username}@example.invalid",
                    password_hash=hash_password(password),
                    is_template_admin=True,
                )
            )
            return f"created admin {admin_username!r}"

    console.print(f"[green]OK[/] {asyncio.run(run())}")
    console.print("Builtin calendars: continuous, taiwan_office")


@app.command("passwd")
def passwd_command(
    username: Annotated[
        str, typer.Argument(help="Account to set a new password for")
    ],
    password: Annotated[
        str,
        typer.Option(
            "--password",
            prompt=True,
            hide_input=True,
            confirmation_prompt=True,
            help="The new password",
        ),
    ] = "",
) -> None:
    """Set an account's password.

    Passwords are stored as argon2id hashes and cannot be read back, so a
    forgotten one can only be replaced. This is the only way to do that
    without editing the database by hand.
    """
    import asyncio

    from sqlalchemy import select

    from app.auth.passwords import hash_password
    from app.db import session_scope
    from app.models import User

    async def run() -> str:
        async with session_scope() as session:
            user = (
                await session.scalars(
                    select(User).where(User.username == username)
                )
            ).one_or_none()
            if user is None:
                raise LookupError(username)
            user.password_hash = hash_password(password)
            return f"password updated for {username!r}"

    try:
        console.print(f"[green]OK[/] {asyncio.run(run())}")
    except LookupError:
        errors.print(f"[red]No account named {username!r}[/]")
        raise typer.Exit(1) from None


def main() -> None:
    sys.exit(app())


if __name__ == "__main__":
    main()
