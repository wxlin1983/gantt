"""Command line entry points.

The phase 1 deliverable: point it at a template plus a set of parameters and
it prints the task graph that a case would be built from, without touching a
database. That makes it the fastest way to check whether the DSL can express
a real flow.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from rich.console import Console
from rich.table import Table

from app.dsl.duration import format_duration
from app.dsl.errors import DslError, Issue
from app.dsl.expansion import expand
from app.dsl.loader import parse_gantt_template, parse_task_template
from app.dsl.schema import ExpansionResult, GanttTemplate, TaskTemplate

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
    dynamic = [
        node.id
        for node in template.flow
        if node.when is not None
    ]
    if dynamic:
        console.print(
            "[yellow]note[/] this template's shape depends on parameters "
            f"({', '.join(dynamic)}); run `expand` with a real parameter set "
            "to check the result"
        )


def main() -> None:
    sys.exit(app())


if __name__ == "__main__":
    main()
