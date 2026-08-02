"""Template, task-template and schedule endpoints (§8.1)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Query, Response, status
from pydantic import Field
from sqlalchemy import select

from app.auth import permissions
from app.models import GanttTemplateRecord, TaskTemplateRecord
from app.services import credentials, schedules
from app.services import templates as template_service

from ..deps import PrincipalDep, SessionDep, UserDep, require
from ..errors import ApiError
from ..schemas import Body as BodyModel

router = APIRouter(tags=["templates"])


class DraftRequest(BodyModel):
    definition: dict[str, Any]
    change_note: str = ""


class PublishRequest(BodyModel):
    change_note: str = ""


class ImportRequest(BodyModel):
    document: str


class ScheduleRequest(BodyModel):
    cron: str
    timezone: str = "Asia/Taipei"
    target_date_offset_seconds: int = Field(default=0, ge=0)
    name_template: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    role_assignments: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True


class TaskTemplateRequest(BodyModel):
    name: str
    display_name: str = ""
    duration_default: str = "1H"
    schedule_mode: str = "continuous"
    para_schema: list[dict[str, Any]] = Field(default_factory=list)
    task_api: str | None = None
    api_mode: str | None = None
    api_config: dict[str, Any] = Field(default_factory=dict)
    api_timeout_s: int = 1800
    api_retry_max: int = 3
    api_retry_interval_s: int = 300
    api_poll_interval_s: int = 60
    allow_manual_override: bool = True
    on_failure: str = "block"
    warn_before_s: int = 7200


class CredentialRequest(BodyModel):
    name: str
    value: str
    description: str = ""


# --- gantt templates -------------------------------------------------------


@router.get("/templates")
async def list_templates(
    session: SessionDep, principal: PrincipalDep
) -> list[dict[str, Any]]:
    require(permissions.can_view(principal), "sign in first")
    return await template_service.list_templates(session)


@router.get("/templates/{name}")
async def get_template(
    name: str, session: SessionDep, principal: PrincipalDep
) -> dict[str, Any]:
    require(permissions.can_view(principal), "sign in first")
    rows = await template_service.versions(session, name)
    if not rows:
        raise ApiError("E_TEMPLATE_NOT_FOUND", f"{name} does not exist")
    published = [row for row in rows if row.status == "published"]
    draft = next((row for row in rows if row.status == "draft"), None)
    latest = published[0] if published else None
    return {
        "name": name,
        "latest_version": latest.version if latest else None,
        "definition": (latest or draft).definition,
        "draft": (
            {"version": draft.version, "definition": draft.definition}
            if draft
            else None
        ),
        "versions": [
            {
                "version": row.version,
                "status": row.status,
                "change_note": row.change_note,
                "published_at": row.published_at,
            }
            for row in rows
        ],
    }


@router.post("/templates/validate")
async def validate_template(
    session: SessionDep,
    principal: PrincipalDep,
    definition: Annotated[dict[str, Any], Body(embed=True)],
) -> dict[str, Any]:
    """Validate without saving, for the editor's live panel (§9.2)."""
    require(permissions.can_view(principal), "sign in first")
    result = await template_service.validate(session, definition)
    return result.as_dict()


@router.put("/templates/{name}/draft")
async def save_draft(
    name: str,
    body: DraftRequest,
    session: SessionDep,
    user: UserDep,
    principal: PrincipalDep,
) -> dict[str, Any]:
    require(
        permissions.can_manage_templates(principal),
        "only a template admin can edit templates",
    )
    definition = {**body.definition, "template_name": name}
    draft = await template_service.save_draft(
        session, definition, user.id, body.change_note
    )
    return {"name": draft.name, "version": draft.version, "status": "draft"}


@router.delete(
    "/templates/{name}/draft", status_code=status.HTTP_204_NO_CONTENT
)
async def discard_draft(
    name: str, session: SessionDep, principal: PrincipalDep
) -> None:
    require(
        permissions.can_manage_templates(principal),
        "only a template admin can edit templates",
    )
    await template_service.discard_draft(session, name)


@router.post("/templates/{name}/publish")
async def publish_template(
    name: str,
    body: PublishRequest,
    session: SessionDep,
    principal: PrincipalDep,
) -> dict[str, Any]:
    require(
        permissions.can_manage_templates(principal),
        "only a template admin can publish templates",
    )
    row = await template_service.publish(session, name, body.change_note)
    return {
        "name": row.name,
        "version": row.version,
        "status": row.status,
        "published_at": row.published_at,
    }


@router.get("/templates/{name}/diff")
async def diff_versions(
    name: str,
    session: SessionDep,
    principal: PrincipalDep,
    from_version: Annotated[int, Query(alias="from")],
    to_version: Annotated[int, Query(alias="to")],
) -> dict[str, Any]:
    require(permissions.can_view(principal), "sign in first")
    left = await template_service.get_version(session, name, from_version)
    right = await template_service.get_version(session, name, to_version)
    return template_service.diff(left.definition, right.definition)


@router.get("/templates/{name}/export")
async def export_template(
    name: str,
    session: SessionDep,
    principal: PrincipalDep,
    version: Annotated[int | None, Query()] = None,
    include_task_templates: Annotated[bool, Query()] = True,
) -> Response:
    require(permissions.can_view(principal), "sign in first")
    document = await template_service.export(
        session, name, version, include_task_templates
    )
    return Response(
        content=document,
        media_type="application/yaml",
        headers={"content-disposition": f'attachment; filename="{name}.yaml"'},
    )


@router.post("/templates/import")
async def import_template(
    body: ImportRequest,
    session: SessionDep,
    user: UserDep,
    principal: PrincipalDep,
) -> dict[str, Any]:
    require(
        permissions.can_manage_templates(principal),
        "only a template admin can import templates",
    )
    report = await template_service.import_document(
        session, body.document, user.id
    )
    return {
        "template_name": report.template_name,
        "draft_version": report.draft_version,
        "task_templates_created": report.task_templates_created,
        "task_templates_differing": report.task_templates_differing,
        "missing_credentials": report.missing_credentials,
    }


@router.get("/templates/{name}/health")
async def template_health(
    name: str,
    session: SessionDep,
    principal: PrincipalDep,
    since: Annotated[datetime | None, Query()] = None,
) -> dict[str, Any]:
    """Planned versus actual durations (§8.6)."""
    require(permissions.can_view(principal), "sign in first")
    return await template_service.health_report(session, name, since)


# --- schedules -------------------------------------------------------------


@router.get("/templates/{name}/schedule")
async def get_schedule(
    name: str, session: SessionDep, principal: PrincipalDep
) -> dict[str, Any] | None:
    require(permissions.can_view(principal), "sign in first")
    from app.models import TemplateSchedule

    row = (
        await session.scalars(
            select(TemplateSchedule).where(
                TemplateSchedule.template_name == name
            )
        )
    ).one_or_none()
    if row is None:
        return None
    return {
        "cron": row.cron,
        "timezone": row.timezone,
        "target_date_offset_seconds": row.target_date_offset_s,
        "name_template": row.name_template,
        "params": row.params,
        "role_assignments": row.role_assignments,
        "enabled": row.enabled,
        "next_run_at": row.next_run_at,
        "last_run_at": row.last_run_at,
        "last_case_id": row.last_case_id,
    }


@router.put("/templates/{name}/schedule")
async def put_schedule(
    name: str,
    body: ScheduleRequest,
    session: SessionDep,
    user: UserDep,
    principal: PrincipalDep,
) -> dict[str, Any]:
    require(
        permissions.can_manage_templates(principal),
        "only a template admin can configure schedules",
    )
    try:
        row = await schedules.upsert(
            session,
            name,
            cron=body.cron,
            timezone=body.timezone,
            target_date_offset_s=body.target_date_offset_seconds,
            name_template=body.name_template,
            params=body.params,
            role_assignments=body.role_assignments,
            enabled=body.enabled,
            created_by_id=user.id,
        )
    except schedules.CronError as exc:
        raise ApiError("E_BAD_CRON", str(exc)) from exc
    return {"cron": row.cron, "next_run_at": row.next_run_at}


# --- task templates --------------------------------------------------------


@router.get("/task-templates")
async def list_task_templates(
    session: SessionDep, principal: PrincipalDep
) -> list[dict[str, Any]]:
    require(permissions.can_view(principal), "sign in first")
    rows = (
        await session.scalars(
            select(TaskTemplateRecord).order_by(TaskTemplateRecord.name)
        )
    ).all()
    return [
        {
            "name": row.name,
            "display_name": row.display_name,
            "duration_default": row.duration_default,
            "schedule_mode": row.schedule_mode,
            "task_api": row.task_api,
            "api_mode": row.api_mode,
            "api_config": row.api_config,
            "on_failure": row.on_failure,
            "allow_manual_override": row.allow_manual_override,
        }
        for row in rows
    ]


@router.put("/task-templates/{name}")
async def put_task_template(
    name: str,
    body: TaskTemplateRequest,
    session: SessionDep,
    user: UserDep,
    principal: PrincipalDep,
) -> dict[str, Any]:
    require(
        permissions.can_manage_templates(principal),
        "only a template admin can edit task templates",
    )
    row = (
        await session.scalars(
            select(TaskTemplateRecord).where(TaskTemplateRecord.name == name)
        )
    ).one_or_none()
    if row is None:
        row = TaskTemplateRecord(name=name, created_by_id=user.id)
        session.add(row)
    for field_name, value in body.model_dump(exclude={"name"}).items():
        setattr(row, field_name, value)
    await session.flush()
    return {"name": row.name}


@router.delete(
    "/task-templates/{name}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_task_template(
    name: str, session: SessionDep, principal: PrincipalDep
) -> None:
    """Remove a task template not referenced by any gantt template (§8.1)."""
    require(
        permissions.can_manage_templates(principal),
        "only a template admin can delete task templates",
    )
    row = (
        await session.scalars(
            select(TaskTemplateRecord).where(TaskTemplateRecord.name == name)
        )
    ).one_or_none()
    if row is None:
        raise ApiError(
            "E_TEMPLATE_NOT_FOUND",
            f"task template {name!r} does not exist",
        )
    # A task template that appears in any gantt template's definition cannot be
    # deleted: the gantt template would silently reference a missing template.
    referencing: list[str] = []
    gantt_rows = (await session.scalars(select(GanttTemplateRecord))).all()
    for gt in gantt_rows:
        flow = gt.definition.get("flow") or gt.definition.get("gantt", {}).get(
            "flow", []
        )
        if not isinstance(flow, list):
            continue
        for item in flow:
            tasks = item.get("tasks", [item]) if isinstance(item, dict) else []
            for task in tasks:
                if (
                    isinstance(task, dict)
                    and task.get("uses", task.get("task_template")) == name
                ):
                    referencing.append(gt.name)
                    break
    if referencing:
        raise ApiError(
            "E_TEMPLATE_IN_USE",
            f"task template {name!r} is referenced by: "
            + ", ".join(sorted(set(referencing))),
            details={"referencing": sorted(set(referencing))},
        )
    await session.delete(row)


@router.get("/handlers")
async def list_handlers(principal: PrincipalDep) -> list[dict[str, Any]]:
    """What `task_api` may be set to (§9.8).

    Listing only what is actually registered is what stops a typo surfacing
    for the first time in a running case.
    """
    require(permissions.can_view(principal), "sign in first")
    return template_service.registered_handlers()


# --- credentials -----------------------------------------------------------


@router.get("/credentials")
async def list_credentials(
    session: SessionDep, principal: PrincipalDep
) -> list[str]:
    """Names only. The values never leave the server."""
    require(
        permissions.can_manage_credentials(principal),
        "only a template admin can manage credentials",
    )
    return await credentials.names(session)


@router.put("/credentials/{name}")
async def put_credential(
    name: str,
    body: CredentialRequest,
    session: SessionDep,
    user: UserDep,
    principal: PrincipalDep,
) -> dict[str, str]:
    require(
        permissions.can_manage_credentials(principal),
        "only a template admin can manage credentials",
    )
    try:
        await credentials.put(
            session, name, body.value, body.description, user.id
        )
    except credentials.CredentialError as exc:
        raise ApiError("E_CREDENTIAL_ERROR", str(exc)) from exc
    return {"name": name}
