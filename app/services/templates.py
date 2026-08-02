"""Template management (implement.md §8.1, §8.6, §8.7, §9).

Drafts are mutable, published versions are not. Editing a published template
means basing a new draft on it, which is what keeps a running case's definition
stable without needing to freeze the table.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import yaml
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dsl.errors import DslError, Issue, warning
from app.dsl.expressions import referenced_params
from app.dsl.graph import find_cycle
from app.dsl.loader import parse_gantt_template, parse_task_template
from app.dsl.schema import OwnerKind
from app.execution.registry import registry as handler_registry
from app.models import (
    CaseStatus,
    CaseTask,
    GanttCase,
    GanttTemplateRecord,
    TaskStatus,
    TaskTemplateRecord,
    TemplateStatus,
)
from app.services import snapshot


class TemplateError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


# --- validation ------------------------------------------------------------


@dataclass(slots=True)
class Validation:
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": [_issue(i) for i in self.errors],
            "warnings": [_issue(i) for i in self.warnings],
        }


def _issue(issue: Issue) -> dict[str, str]:
    return {
        "code": issue.code,
        "message": issue.message,
        "path": issue.path,
        "severity": issue.severity,
    }


async def validate(
    session: AsyncSession, definition: dict[str, Any]
) -> Validation:
    """Check a definition without saving it (§4.7).

    Only syntax and references can be settled here. A template using `when`
    changes shape with its parameters, so a clean result does not prove the
    expanded graph is sound -- which is why the editor pushes people towards a
    trial run.
    """
    result = Validation()
    try:
        template = parse_gantt_template(definition)
    except DslError as exc:
        result.errors.extend(exc.issues)
        return result

    known_templates = set(
        (await session.scalars(select(TaskTemplateRecord.name))).all()
    )
    ids = [node.id for node in template.flow]
    declared = {param.name for param in template.template_para}
    roles = {role.name for role in template.roles}

    for node in template.flow:
        if node.uses and node.uses not in known_templates:
            result.errors.append(
                Issue(
                    "E_UNKNOWN_TASK_TEMPLATE",
                    f"`uses: {node.uses}` is not a known task template",
                    "error",
                    node.path,
                )
            )
        for ref in node.requirement:
            if ref.task not in ids:
                result.errors.append(
                    Issue(
                        "E_UNKNOWN_REQUIREMENT",
                        f"requirement `{ref.task}` is not a task here",
                        "error",
                        node.path,
                    )
                )
        if node.owner and node.owner.kind is OwnerKind.ROLE:
            if node.owner.value not in roles:
                result.errors.append(
                    Issue(
                        "E_UNKNOWN_ROLE",
                        f"owner.role `{node.owner.value}` is not declared",
                        "error",
                        node.path,
                    )
                )

    cycle = find_cycle(
        ids,
        [
            (ref.task, node.id)
            for node in template.flow
            for ref in node.requirement
        ],
    )
    if cycle:
        result.errors.append(
            Issue(
                "E_CYCLE",
                "dependency cycle: " + " -> ".join(cycle),
                "error",
                "flow",
            )
        )

    result.warnings.extend(_warn(template, declared, roles, known_templates))
    return result


def _warn(template, declared, roles, known_templates) -> list[Issue]:
    issues: list[Issue] = []
    used_params: set[str] = set()
    used_roles: set[str] = set()
    successors: set[str] = set()
    dynamic: list[str] = []

    for node in template.flow:
        for value in (
            node.duration,
            node.label,
            node.group,
            node.when,
            *(node.task_para or {}).values(),
        ):
            used_params |= referenced_params(value)
        if node.owner and node.owner.kind is OwnerKind.ROLE:
            used_roles.add(node.owner.value)
        for ref in node.requirement:
            successors.add(ref.task)
        if node.when is not None:
            dynamic.append(node.id)
        if node.duration in (0, "0", None) and not node.uses:
            issues.append(
                warning("W_ZERO_DURATION", "duration is zero", node.path)
            )
        source_api = None
        if node.uses in known_templates:
            source_api = None  # resolved by the caller's registry check
        del source_api

    for name in sorted(declared - used_params):
        issues.append(
            warning(
                "W_UNUSED_PARAM",
                f"parameter `{name}` is never referenced",
                f"template_para.{name}",
            )
        )
    for name in sorted(roles - used_roles):
        issues.append(
            warning(
                "W_UNUSED_ROLE",
                f"role `{name}` is never used",
                f"roles.{name}",
            )
        )

    sinks = [node.id for node in template.flow if node.id not in successors]
    if len(sinks) > 1:
        issues.append(
            warning(
                "W_MULTIPLE_SINKS",
                f"{len(sinks)} tasks have no successor, so each aligns to the "
                f"target date: {', '.join(sinks)}",
                "flow",
            )
        )
    conditional_sinks = [node for node in dynamic if node in sinks]
    if conditional_sinks:
        issues.append(
            warning(
                "W_CONDITIONAL_SINK",
                "a conditional task is also an endpoint, so the flow's "
                f"ending changes with its parameters: {conditional_sinks}",
                "flow",
            )
        )
    if dynamic:
        issues.append(
            warning(
                "W_SHAPE_DEPENDS_ON_PARAMS",
                "this template's shape depends on its parameters "
                f"({', '.join(dynamic)}); run a trial before publishing",
                "flow",
            )
        )
    return issues


# --- drafts and versions ---------------------------------------------------


async def get_draft(
    session: AsyncSession, name: str
) -> GanttTemplateRecord | None:
    return (
        await session.scalars(
            select(GanttTemplateRecord).where(
                GanttTemplateRecord.name == name,
                GanttTemplateRecord.status == TemplateStatus.DRAFT,
            )
        )
    ).one_or_none()


async def versions(
    session: AsyncSession, name: str
) -> list[GanttTemplateRecord]:
    rows = await session.scalars(
        select(GanttTemplateRecord)
        .where(GanttTemplateRecord.name == name)
        .order_by(GanttTemplateRecord.version.desc())
    )
    return list(rows.all())


async def get_version(
    session: AsyncSession, name: str, version: int
) -> GanttTemplateRecord:
    row = (
        await session.scalars(
            select(GanttTemplateRecord).where(
                GanttTemplateRecord.name == name,
                GanttTemplateRecord.version == version,
            )
        )
    ).one_or_none()
    if row is None:
        raise TemplateError(
            "E_TEMPLATE_NOT_FOUND", f"{name} v{version} does not exist"
        )
    return row


async def list_templates(session: AsyncSession) -> list[dict[str, Any]]:
    """Latest published version of each template, with usage counts."""
    latest = (
        select(
            GanttTemplateRecord.name,
            func.max(GanttTemplateRecord.version).label("version"),
        )
        .where(GanttTemplateRecord.status == TemplateStatus.PUBLISHED)
        .group_by(GanttTemplateRecord.name)
        .subquery()
    )
    rows = (
        await session.execute(
            select(GanttTemplateRecord).join(
                latest,
                (GanttTemplateRecord.name == latest.c.name)
                & (GanttTemplateRecord.version == latest.c.version),
            )
        )
    ).scalars().all()

    counts = dict(
        (
            await session.execute(
                select(GanttCase.template_name, func.count())
                .where(GanttCase.status == CaseStatus.ACTIVE)
                .group_by(GanttCase.template_name)
            )
        ).all()
    )
    drafts = set(
        (
            await session.scalars(
                select(GanttTemplateRecord.name).where(
                    GanttTemplateRecord.status == TemplateStatus.DRAFT
                )
            )
        ).all()
    )

    return [
        {
            "name": row.name,
            "version": row.version,
            "description": (row.definition or {}).get("description", ""),
            "step_count": len((row.definition or {}).get("flow") or []),
            "active_cases": counts.get(row.name, 0),
            "has_draft": row.name in drafts,
            "published_at": row.published_at,
        }
        for row in rows
    ]


async def save_draft(
    session: AsyncSession,
    definition: dict[str, Any],
    actor_id: int | None = None,
    change_note: str = "",
) -> GanttTemplateRecord:
    """Create or overwrite the draft for a template name.

    Validation runs first: a draft that cannot be parsed is not worth storing,
    and storing it would make the editor's next load fail.
    """
    result = await validate(session, definition)
    if not result.ok:
        raise DslError(result.errors)

    name = definition["template_name"]
    draft = await get_draft(session, name)
    if draft is None:
        draft = GanttTemplateRecord(
            name=name,
            version=await _next_version(session, name),
            status=TemplateStatus.DRAFT,
            created_by_id=actor_id,
        )
        session.add(draft)
    draft.definition = definition
    draft.change_note = change_note or draft.change_note
    await session.flush()
    return draft


async def _next_version(session: AsyncSession, name: str) -> int:
    highest = (
        await session.scalars(
            select(func.max(GanttTemplateRecord.version)).where(
                GanttTemplateRecord.name == name
            )
        )
    ).first()
    return (highest or 0) + 1


async def publish(
    session: AsyncSession, name: str, change_note: str = ""
) -> GanttTemplateRecord:
    """Turn the draft into an immutable published version."""
    draft = await get_draft(session, name)
    if draft is None:
        raise TemplateError(
            "E_NO_DRAFT", f"{name} has no draft to publish"
        )
    result = await validate(session, draft.definition)
    if not result.ok:
        raise DslError(result.errors)

    draft.status = TemplateStatus.PUBLISHED
    draft.published_at = datetime.now(tz=UTC)
    draft.change_note = change_note or draft.change_note
    # Stamp the version into the definition so a snapshot carries it too.
    draft.definition = {**draft.definition, "version": draft.version}
    await session.flush()
    return draft


async def discard_draft(session: AsyncSession, name: str) -> None:
    draft = await get_draft(session, name)
    if draft is None:
        raise TemplateError("E_NO_DRAFT", f"{name} has no draft")
    await session.delete(draft)
    await session.flush()


def diff(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Structural difference between two definitions.

    Reported per task rather than per line: "the review step got four hours
    longer" is what a reviewer needs, not a YAML hunk.
    """
    def nodes(definition: dict[str, Any]) -> dict[str, dict]:
        flow = definition.get("flow") or []
        flat: list[dict] = []
        for entry in flow:
            if isinstance(entry, dict) and "tasks" in entry:
                flat.extend(entry.get("tasks") or [])
            else:
                flat.append(entry)
        return {
            (node.get("id") or node.get("task_name")): node for node in flat
        }

    before, after = nodes(left), nodes(right)
    changed = []
    for key in sorted(set(before) & set(after)):
        fields = {
            field: (before[key].get(field), after[key].get(field))
            for field in set(before[key]) | set(after[key])
            if before[key].get(field) != after[key].get(field)
        }
        if fields:
            changed.append({"id": key, "fields": fields})

    return {
        "added": sorted(set(after) - set(before)),
        "removed": sorted(set(before) - set(after)),
        "changed": changed,
        "buffer": (left.get("buffer"), right.get("buffer"))
        if left.get("buffer") != right.get("buffer")
        else None,
    }


# --- export and import -----------------------------------------------------


async def export(
    session: AsyncSession,
    name: str,
    version: int | None = None,
    include_task_templates: bool = True,
) -> str:
    """Render a template as portable YAML (§8.7).

    Credentials are referenced by name only, so an export can be committed to
    version control without leaking a token.
    """
    row = (
        await get_version(session, name, version)
        if version is not None
        else await _latest_published(session, name)
    )
    document: dict[str, Any] = {"gantt": row.definition}

    if include_task_templates:
        referenced = sorted(
            {
                node.get("uses") or node.get("task_template")
                for node in _flat_nodes(row.definition)
                if node.get("uses") or node.get("task_template")
            }
        )
        rows = (
            await session.scalars(
                select(TaskTemplateRecord).where(
                    TaskTemplateRecord.name.in_(referenced)
                )
            )
        ).all()
        document["task_templates"] = [
            snapshot.task_template_to_dsl(item) for item in rows
        ]

    return yaml.safe_dump(
        document, sort_keys=False, allow_unicode=True, width=100
    )


def _flat_nodes(definition: dict[str, Any]) -> list[dict]:
    flow = definition.get("flow") or []
    flat: list[dict] = []
    for entry in flow:
        if isinstance(entry, dict) and "tasks" in entry:
            flat.extend(entry.get("tasks") or [])
        else:
            flat.append(entry)
    return flat


async def _latest_published(
    session: AsyncSession, name: str
) -> GanttTemplateRecord:
    row = (
        await session.scalars(
            select(GanttTemplateRecord)
            .where(
                GanttTemplateRecord.name == name,
                GanttTemplateRecord.status == TemplateStatus.PUBLISHED,
            )
            .order_by(GanttTemplateRecord.version.desc())
        )
    ).first()
    if row is None:
        raise TemplateError(
            "E_TEMPLATE_NOT_FOUND", f"{name} has no published version"
        )
    return row


@dataclass(slots=True)
class ImportReport:
    template_name: str
    draft_version: int
    task_templates_created: list[str] = field(default_factory=list)
    task_templates_differing: list[str] = field(default_factory=list)
    missing_credentials: list[str] = field(default_factory=list)


async def import_document(
    session: AsyncSession, document: str, actor_id: int | None = None
) -> ImportReport:
    """Import YAML as a draft (§8.7).

    Always a draft, never a direct overwrite of a published version, and
    validated before anything is written -- a failed import leaves no half
    template behind.
    """
    try:
        loaded = yaml.safe_load(document)
    except yaml.YAMLError as exc:
        raise TemplateError("E_BAD_YAML", f"invalid YAML: {exc}") from exc
    if not isinstance(loaded, dict):
        raise TemplateError("E_BAD_YAML", "document root must be a mapping")

    gantt = loaded.get("gantt") or loaded
    incoming_tasks = loaded.get("task_templates") or []

    created: list[str] = []
    differing: list[str] = []
    for entry in incoming_tasks:
        parsed = parse_task_template(entry)
        existing = (
            await session.scalars(
                select(TaskTemplateRecord).where(
                    TaskTemplateRecord.name == parsed.id
                )
            )
        ).one_or_none()
        if existing is None:
            session.add(
                TaskTemplateRecord(
                    name=parsed.id,
                    display_name=parsed.label,
                    duration_default=str(parsed.default_duration),
                    schedule_mode=parsed.schedule_mode,
                    para_schema=[
                        param.model_dump() for param in parsed.task_para
                    ],
                    task_api=parsed.task_api or None,
                    api_mode=parsed.api_mode,
                    # The calendar rides in `api_config`, the same blob
                    # `case_tasks` already carries it in. There is no column
                    # for it, so importing a task template that named one
                    # silently dropped it and the task fell back to the
                    # default office calendar.
                    api_config=(
                        {**parsed.api_config, "calendar": parsed.calendar}
                        if parsed.calendar
                        else parsed.api_config
                    ),
                    allow_manual_override=parsed.allow_manual_override,
                    on_failure=parsed.on_failure,
                    created_by_id=actor_id,
                )
            )
            created.append(parsed.id)
        elif existing.duration_default != str(parsed.default_duration):
            # Reported, not overwritten: silently changing a shared task
            # template would alter other flows too.
            differing.append(parsed.id)
    await session.flush()

    draft = await save_draft(session, gantt, actor_id, "imported")

    missing = sorted(
        {
            (entry.get("api_config") or {}).get("auth_ref")
            for entry in incoming_tasks
            if (entry.get("api_config") or {}).get("auth_ref")
        }
        - set(await _credential_names(session))
    )
    return ImportReport(
        template_name=draft.name,
        draft_version=draft.version,
        task_templates_created=created,
        task_templates_differing=differing,
        missing_credentials=missing,
    )


async def _credential_names(session: AsyncSession) -> list[str]:
    from app.services import credentials

    return await credentials.names(session)


# --- health report ---------------------------------------------------------


async def health_report(
    session: AsyncSession, name: str, since: datetime | None = None
) -> dict[str, Any]:
    """Compare planned against actual durations (§8.6).

    Every number here comes from data the system already records; nobody has to
    start collecting anything. It is the only place that answers "which step is
    actually slowing us down", and it turns a template from write-once into
    something that can be calibrated.
    """
    conditions = [
        GanttCase.template_name == name,
        CaseTask.status == TaskStatus.DONE,
        CaseTask.actual_start.is_not(None),
        CaseTask.actual_end.is_not(None),
        # Tasks inserted mid-flight have no plan to be measured against.
        CaseTask.baseline_start.is_not(None),
    ]
    if since is not None:
        conditions.append(GanttCase.created_at >= since)

    rows = (
        await session.execute(
            select(
                CaseTask.name,
                CaseTask.display_name,
                CaseTask.duration_seconds,
                CaseTask.actual_start,
                CaseTask.actual_end,
                CaseTask.is_on_critical_path,
            )
            .join(GanttCase, GanttCase.id == CaseTask.case_id)
            .where(*conditions)
        )
    ).all()

    cases = (
        await session.execute(
            select(GanttCase.id, GanttCase.forecast_end, GanttCase.target_date)
            .where(
                GanttCase.template_name == name,
                GanttCase.status == CaseStatus.COMPLETED,
            )
        )
    ).all()

    grouped: dict[str, dict[str, Any]] = {}
    for task_name, label, planned, start, end, critical in rows:
        entry = grouped.setdefault(
            task_name,
            {
                "task_id": task_name,
                "label": label or task_name,
                "planned_duration_seconds": planned,
                "actuals": [],
                "critical_hits": 0,
            },
        )
        entry["actuals"].append(int((end - start).total_seconds()))
        entry["critical_hits"] += 1 if critical else 0

    tasks = []
    for entry in grouped.values():
        actuals = sorted(entry["actuals"])
        sample = len(actuals)
        median = int(statistics.median(actuals))
        p80 = actuals[min(int(sample * 0.8), sample - 1)]
        planned = entry["planned_duration_seconds"] or 0
        overruns = sum(1 for value in actuals if value > planned)
        tasks.append(
            {
                "task_id": entry["task_id"],
                "label": entry["label"],
                "planned_duration_seconds": planned,
                "sample_size": sample,
                "actual_median_seconds": median,
                "actual_p80_seconds": p80,
                "overrun_ratio": round(overruns / sample, 3),
                "on_critical_path_ratio": round(
                    entry["critical_hits"] / sample, 3
                ),
                # Only suggest a change once there is enough evidence and the
                # gap is big enough to matter.
                "suggestion": (
                    f"median is {median / 3600:.1f}h against a planned "
                    f"{planned / 3600:.1f}h; consider raising it"
                    if sample >= 10 and planned and median > planned * 1.2
                    else ""
                ),
            }
        )

    on_time = sum(
        1
        for _, forecast_end, target in cases
        if forecast_end is not None and forecast_end <= target
    )
    return {
        "template_name": name,
        "case_count": len(cases),
        "on_time_ratio": round(on_time / len(cases), 3) if cases else None,
        "tasks": sorted(
            tasks, key=lambda item: -item["on_critical_path_ratio"]
        ),
        "bottlenecks": [
            {
                "task_id": item["task_id"],
                "reason": (
                    f"on the critical path in "
                    f"{item['on_critical_path_ratio']:.0%} of cases and over "
                    f"plan in {item['overrun_ratio']:.0%}"
                ),
            }
            for item in tasks
            if item["on_critical_path_ratio"] > 0.5
            and item["overrun_ratio"] > 0.5
        ],
    }


def registered_handlers() -> list[dict[str, Any]]:
    """What the task template editor may offer in its dropdown (§9.8)."""
    return handler_registry.describe()
