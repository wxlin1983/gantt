"""YAML entry points for the DSL.

Thin wrapper over the pydantic schema: read text, unwrap the optional root key
(``gantt:`` / ``task:``), and convert pydantic failures into domain issues.
"""

from __future__ import annotations

from typing import Any

import yaml
from pydantic import ValidationError

from .errors import DslError
from .schema import (
    GanttTemplate,
    TaskTemplate,
    issues_from_validation_error,
)


def _load(document: str | dict[str, Any], root_key: str) -> dict[str, Any]:
    """Accept YAML text or a mapping, with or without the root wrapper key."""
    if isinstance(document, str):
        try:
            loaded = yaml.safe_load(document)
        except yaml.YAMLError as exc:
            raise DslError.single(
                "E_BAD_YAML", f"invalid YAML: {exc}", root_key
            ) from exc
    else:
        loaded = document

    if loaded is None:
        raise DslError.single("E_MISSING_FIELD", "document is empty", root_key)
    if not isinstance(loaded, dict):
        raise DslError.single(
            "E_MISSING_FIELD", "document root must be a mapping", root_key
        )

    inner = loaded.get(root_key)
    return inner if isinstance(inner, dict) else loaded


def parse_gantt_template(document: str | dict[str, Any]) -> GanttTemplate:
    """Parse a gantt template from YAML text or a mapping."""
    data = _load(document, "gantt")
    try:
        return GanttTemplate.model_validate(data)
    except ValidationError as exc:
        raise DslError(issues_from_validation_error(exc, "gantt")) from exc


def parse_task_template(document: str | dict[str, Any]) -> TaskTemplate:
    """Parse a task template from YAML text or a mapping."""
    data = _load(document, "task")
    try:
        return TaskTemplate.model_validate(data)
    except ValidationError as exc:
        raise DslError(issues_from_validation_error(exc, "task")) from exc


def parse_task_templates(
    documents: list[str | dict[str, Any]],
) -> dict[str, TaskTemplate]:
    """Parse many task templates into the ``{id: template}`` map ``expand``
    expects."""
    issues = []
    templates: dict[str, TaskTemplate] = {}
    for document in documents:
        try:
            template = parse_task_template(document)
        except DslError as exc:
            issues.extend(exc.issues)
            continue
        templates[template.id] = template
    if issues:
        raise DslError(issues)
    return templates
