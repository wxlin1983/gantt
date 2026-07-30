"""Case snapshots (implement.md §4.8).

A snapshot is self-contained: the gantt template, every task template it
references, and every calendar those name. Once written it is never updated,
so a template published tomorrow cannot change how a case running today was
planned.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dsl.loader import parse_gantt_template, parse_task_template
from app.dsl.schema import GanttTemplate, TaskTemplate
from app.models import TaskTemplateRecord
from app.services import calendars as calendar_service

SNAPSHOT_VERSION = 1


class SnapshotError(Exception):
    """The snapshot could not be built or read back."""


def task_template_to_dsl(row: TaskTemplateRecord) -> dict[str, Any]:
    """Render a stored task template back into DSL form.

    Enum columns are coerced to plain strings. They are ``StrEnum``, so JSON
    accepts them silently, but ``yaml.safe_dump`` refuses to represent them --
    which would break template export. Normalising at the source keeps the
    snapshot and the export byte-identical in what they contain.
    """
    return {
        "id": row.name,
        "label": row.display_name or "",
        "default_duration": row.duration_default,
        "schedule_mode": str(row.schedule_mode),
        "task_para": row.para_schema or [],
        "task_api": row.task_api or "",
        "api_mode": str(row.api_mode) if row.api_mode else None,
        "api_config": row.api_config or {},
        "allow_manual_override": row.allow_manual_override,
        "on_failure": str(row.on_failure),
    }


async def build(
    session: AsyncSession,
    definition: dict[str, Any],
    template_version: int,
) -> dict[str, Any]:
    """Freeze everything a case needs to be rescheduled later.

    Calendars are frozen alongside the templates. Without that, editing the
    holiday table would silently change the arithmetic behind an existing
    case's dates.
    """
    template = parse_gantt_template(definition)
    referenced = sorted({node.uses for node in template.flow if node.uses})

    rows = (
        await session.scalars(
            select(TaskTemplateRecord).where(
                TaskTemplateRecord.name.in_(referenced)
            )
        )
    ).all()
    found = {row.name: row for row in rows}
    missing = [name for name in referenced if name not in found]
    if missing:
        raise SnapshotError(
            f"task templates not found: {', '.join(missing)}"
        )

    return {
        "snapshot_version": SNAPSHOT_VERSION,
        "captured_at": datetime.now(tz=UTC).isoformat(),
        "template_version": template_version,
        "gantt": definition,
        "task_templates": {
            name: task_template_to_dsl(row) for name, row in found.items()
        },
        "calendars": await calendar_service.load_definitions(session),
    }


def read(
    snapshot: dict[str, Any],
) -> tuple[GanttTemplate, dict[str, TaskTemplate]]:
    """Parse a snapshot back into the DSL objects the pipeline expects."""
    version = snapshot.get("snapshot_version")
    if version != SNAPSHOT_VERSION:
        raise SnapshotError(
            f"unsupported snapshot version: {version!r} "
            f"(this build reads {SNAPSHOT_VERSION})"
        )
    template = parse_gantt_template(snapshot["gantt"])
    task_templates = {
        name: parse_task_template(document)
        for name, document in (snapshot.get("task_templates") or {}).items()
    }
    return template, task_templates
