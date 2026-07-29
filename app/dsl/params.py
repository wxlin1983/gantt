"""Case parameter and role validation (implement.md §4.15 step 3)."""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import TypeAdapter, ValidationError

from .errors import Issue, error
from .schema import GanttTemplate, ParamDef, ParamType

#: Coercion is delegated to pydantic so that "3" -> 3 and "2026-08-15" -> date
#: behave the same way here as everywhere else in the stack.
_ADAPTERS: dict[ParamType, TypeAdapter] = {
    ParamType.INT: TypeAdapter(int),
    ParamType.FLOAT: TypeAdapter(float),
    ParamType.STR: TypeAdapter(str),
    ParamType.BOOL: TypeAdapter(bool),
    ParamType.DATE: TypeAdapter(date),
}

_EXPECTED = {
    ParamType.INT: "an integer",
    ParamType.FLOAT: "a number",
    ParamType.STR: "a string",
    ParamType.BOOL: "a boolean",
    ParamType.DATE: "an ISO date",
}


def _coerce(
    value: Any, param: ParamDef, path: str
) -> tuple[Any, Issue | None]:
    if param.type is ParamType.ENUM:
        if value not in param.choices:
            allowed = ", ".join(repr(c) for c in param.choices)
            return value, error(
                "E_BAD_PARAM_VALUE",
                f"`{param.name}` must be one of: {allowed} (got {value!r})",
                path,
            )
        return value, None

    # bool is a subclass of int, and pydantic would happily turn True into 1
    if isinstance(value, bool) and param.type is not ParamType.BOOL:
        return value, error(
            "E_BAD_PARAM_VALUE",
            f"`{param.name}` must be {_EXPECTED[param.type]} (got {value!r})",
            path,
        )

    try:
        return _ADAPTERS[param.type].validate_python(value), None
    except ValidationError:
        return value, error(
            "E_BAD_PARAM_VALUE",
            f"`{param.name}` must be {_EXPECTED[param.type]} (got {value!r})",
            path,
        )


def _check_range(value: Any, param: ParamDef, path: str) -> Issue | None:
    rules = param.validation
    if not rules or not isinstance(value, int | float):
        return None
    minimum, maximum = rules.get("min"), rules.get("max")
    if minimum is not None and value < minimum:
        return error(
            "E_BAD_PARAM_VALUE",
            f"`{param.name}` must be >= {minimum} (got {value})",
            path,
        )
    if maximum is not None and value > maximum:
        return error(
            "E_BAD_PARAM_VALUE",
            f"`{param.name}` must be <= {maximum} (got {value})",
            path,
        )
    return None


def resolve_params(
    template: GanttTemplate, supplied: dict[str, Any]
) -> tuple[dict[str, Any], list[Issue]]:
    """Fill defaults, coerce types and validate against ``template_para``.

    Unknown keys are reported rather than ignored: a typo in a parameter name
    would otherwise fall back to the default and quietly produce a schedule
    the user never asked for.
    """
    issues: list[Issue] = []
    resolved: dict[str, Any] = {}

    for param in template.template_para:
        path = f"params.{param.name}"
        given = supplied.get(param.name)

        if given is not None:
            value, issue = _coerce(given, param, path)
            if issue is not None:
                issues.append(issue)
                continue
            range_issue = _check_range(value, param, path)
            if range_issue is not None:
                issues.append(range_issue)
                continue
            resolved[param.name] = value
        elif param.default is not None:
            value, issue = _coerce(param.default, param, path)
            resolved[param.name] = param.default if issue else value
        elif param.required:
            issues.append(
                error(
                    "E_MISSING_PARAM",
                    f"`{param.name}` is required and has no default",
                    path,
                )
            )
        else:
            resolved[param.name] = None

    known = {p.name for p in template.template_para}
    issues.extend(
        error(
            "E_UNKNOWN_PARAM",
            f"`{name}` is not declared in template_para",
            f"params.{name}",
        )
        for name in supplied
        if name not in known
    )
    return resolved, issues


def resolve_roles(
    template: GanttTemplate, supplied: dict[str, str]
) -> tuple[dict[str, str], list[Issue]]:
    """Check that every required role has been assigned (§4.10)."""
    issues: list[Issue] = []
    resolved: dict[str, str] = {}

    for role in template.roles:
        assigned = supplied.get(role.name)
        if assigned:
            resolved[role.name] = assigned
        elif role.required:
            issues.append(
                error(
                    "E_MISSING_ROLE",
                    f"role `{role.name}` must be assigned before the case can "
                    "be created",
                    f"roles.{role.name}",
                )
            )

    known = {r.name for r in template.roles}
    issues.extend(
        error(
            "E_UNKNOWN_ROLE",
            f"`{name}` is not declared in roles",
            f"roles.{name}",
        )
        for name in supplied
        if name not in known
    )
    return resolved, issues
