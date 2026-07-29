"""Pydantic schema for the DSL (implement.md §4).

This module owns *shape*: field names, types, defaults, legacy aliases and
structural normalisation. It deliberately does not own *meaning* — unknown
requirement targets, dependency cycles and expression evaluation all need the
whole document plus case parameters, so they live in ``expansion.py``.

Fields that may contain ``{{ }}`` expressions are typed loosely (``str | int``
rather than a parsed duration), because their real value is only knowable once
parameters are supplied. That is an inherent limit of a template language, not
an oversight.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Self

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from .errors import Issue, Severity

# Legacy field names kept working forever (§4.9). Canonical name -> alias.
FLOW_ALIASES = {
    "id": "task_name",
    "uses": "task_template",
    "label": "display_name",
    "duration": "target_duration",
    "owner": "task_owner",
    "group": "task_group",
}

TASK_TEMPLATE_ALIASES = {
    "id": "task_name",
    "label": "display_name",
    "default_duration": "task_duration_default",
}


def _is_blank_requirement(value: Any) -> bool:
    """The documented ways to say "this task has no predecessor" (§4.3).

    Cannot be a set membership test: `requirement` may legitimately hold a
    list or mapping, and those are unhashable.
    """
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"", "none"}
    return False


def _aliased(canonical: str, aliases: dict[str, str]) -> AliasChoices:
    return AliasChoices(canonical, aliases[canonical])


class _Base(BaseModel):
    # extra="forbid" turns a typo such as `requirment:` into a loud error
    # instead of a silently ignored field, which matters a great deal for a
    # format authored by hand.
    model_config = ConfigDict(
        extra="forbid", populate_by_name=True, use_enum_values=False
    )


# --- enums -----------------------------------------------------------------


class ParamType(StrEnum):
    INT = "int"
    FLOAT = "float"
    STR = "str"
    BOOL = "bool"
    DATE = "date"
    ENUM = "enum"


class ScheduleMode(StrEnum):
    CONTINUOUS = "continuous"
    BUSINESS = "business"


class FailurePolicy(StrEnum):
    BLOCK = "block"
    CONTINUE = "continue"
    CANCEL_CASE = "cancel_case"


class ApiMode(StrEnum):
    TRIGGER_POLL = "trigger_poll"
    TRIGGER_CALLBACK = "trigger_callback"
    POLL_ONLY = "poll_only"


class OwnerKind(StrEnum):
    #: A username, or a string containing expressions that render to one.
    LITERAL = "literal"
    ROLE = "role"
    GROUP_LEAD = "group_lead"
    SAME_AS = "same_as"


# --- leaf models -----------------------------------------------------------


class OwnerSpec(_Base):
    """One of the five owner forms in §4.10."""

    kind: OwnerKind
    value: str

    @model_validator(mode="before")
    @classmethod
    def _accept_shorthand(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {"kind": OwnerKind.LITERAL, "value": data}
        if isinstance(data, dict) and "kind" not in data:
            if len(data) != 1:
                raise ValueError(
                    "E_MISSING_FIELD: owner mapping must hold exactly one of "
                    f"role, group_lead, same_as (got {sorted(data)})"
                )
            key, target = next(iter(data.items()))
            if key == OwnerKind.LITERAL:
                raise ValueError(
                    "E_MISSING_FIELD: write a literal owner as a plain string"
                )
            return {"kind": key, "value": target}
        return data

    def __str__(self) -> str:
        if self.kind is OwnerKind.LITERAL:
            return self.value
        return f"{self.kind.value}:{self.value}"


class ParamDef(_Base):
    name: str = Field(validation_alias=AliasChoices("para_name", "name"))
    type: ParamType = Field(
        default=ParamType.STR,
        validation_alias=AliasChoices("para_type", "type"),
    )
    default: Any = Field(
        default=None, validation_alias=AliasChoices("para_default", "default")
    )
    required: bool = True
    description: str = ""
    group: str = ""
    choices: list[Any] = Field(default_factory=list)
    validation: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _enum_needs_choices(self) -> Self:
        if self.type is ParamType.ENUM and not self.choices:
            raise ValueError(
                f"E_MISSING_FIELD: enum parameter `{self.name}` needs "
                "`choices`"
            )
        return self


class RoleDef(_Base):
    name: str
    display_name: str = ""
    required: bool = True
    default_group: str = ""


class RequirementRef(_Base):
    """One dependency edge as written.

    ``lag`` stays raw so an expression such as ``{{ para.wait }}H`` survives
    until parameters are known.
    """

    task: str
    lag: str | int = 0

    @model_validator(mode="before")
    @classmethod
    def _accept_shorthand(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {"task": data}
        return data


class ScheduleSpec(_Base):
    """Recurring case creation (§4.16)."""

    cron: str
    timezone: str = "Asia/Taipei"
    target_date_offset: str | int = "0S"
    name_template: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    role_assignments: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True


# --- templates -------------------------------------------------------------


class TaskTemplate(_Base):
    """A reusable task definition (§4.2)."""

    id: str = Field(validation_alias=_aliased("id", TASK_TEMPLATE_ALIASES))
    label: str = Field(
        default="", validation_alias=_aliased("label", TASK_TEMPLATE_ALIASES)
    )
    default_duration: str | int = Field(
        default=0,
        validation_alias=_aliased("default_duration", TASK_TEMPLATE_ALIASES),
    )
    schedule_mode: ScheduleMode = ScheduleMode.CONTINUOUS
    calendar: str = "continuous"
    default_owner: OwnerSpec | None = None
    warn_before: str | int = "2H"
    task_para: list[ParamDef] = Field(default_factory=list)
    task_api: str = ""
    api_mode: ApiMode | None = None
    api_config: dict[str, Any] = Field(default_factory=dict)
    api_timeout: str | int = "30M"
    api_retry_max: int = 3
    api_retry_interval: str | int = "5M"
    api_poll_interval: str | int = "60S"
    allow_manual_override: bool = True
    on_failure: FailurePolicy = FailurePolicy.BLOCK

    @model_validator(mode="before")
    @classmethod
    def _check_aliases(cls, data: Any) -> Any:
        return _reject_alias_conflicts(data, TASK_TEMPLATE_ALIASES)

    @model_validator(mode="after")
    def _default_api_mode(self) -> Self:
        # An api-backed task without an explicit mode gets the documented
        # default rather than silently never being triggered.
        if self.task_api and self.api_mode is None:
            self.api_mode = ApiMode.TRIGGER_POLL
        return self

    @property
    def display_label(self) -> str:
        return self.label or self.id


class FlowNode(_Base):
    """One entry in ``flow`` before expansion (§4.1)."""

    id: str = Field(validation_alias=_aliased("id", FLOW_ALIASES))
    uses: str = Field(
        default="", validation_alias=_aliased("uses", FLOW_ALIASES)
    )
    label: str = Field(
        default="", validation_alias=_aliased("label", FLOW_ALIASES)
    )
    owner: OwnerSpec | None = Field(
        default=None, validation_alias=_aliased("owner", FLOW_ALIASES)
    )
    group: str = Field(
        default="", validation_alias=_aliased("group", FLOW_ALIASES)
    )
    requirement: list[RequirementRef] = Field(default_factory=list)
    duration: str | int | None = Field(
        default=None, validation_alias=_aliased("duration", FLOW_ALIASES)
    )
    when: str | None = None
    for_each: str | None = None
    id_suffix: str | int | None = None
    sequential: bool = False
    schedule_mode: ScheduleMode | None = None
    calendar: str | None = None
    task_para: dict[str, Any] = Field(default_factory=dict)
    on_failure: FailurePolicy | None = None
    optional: bool = False

    # Filled in while flattening phases; not user-writable.
    phase: str = Field(default="", exclude=True)
    source_index: int = Field(default=0, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def _normalise(cls, data: Any) -> Any:
        data = _reject_alias_conflicts(data, FLOW_ALIASES)
        if isinstance(data, dict) and "requirement" in data:
            data = {**data, "requirement": _normalise_requirement(data)}
        return data

    @field_validator("sequential")
    @classmethod
    def _sequential_needs_for_each(cls, value: bool, info) -> bool:
        if value and not info.data.get("for_each"):
            raise ValueError(
                "E_MISSING_FIELD: `sequential` only applies to a `for_each` "
                "node"
            )
        return value

    @property
    def path(self) -> str:
        return f"flow[{self.source_index}] ({self.id})"


class PhaseSection(_Base):
    """A visual grouping of tasks (§4.14)."""

    phase: str
    default_owner: OwnerSpec | None = None
    tasks: list[FlowNode] = Field(default_factory=list)


class GanttTemplate(_Base):
    """A parsed gantt template (§4.1)."""

    template_name: str
    dsl_version: int = 1
    version: int = 1
    description: str = ""
    buffer: str | int = 0
    schedule: ScheduleSpec | None = None
    roles: list[RoleDef] = Field(default_factory=list)
    template_para: list[ParamDef] = Field(default_factory=list)
    flow: list[FlowNode] = Field(default_factory=list)
    default_owner: OwnerSpec | None = None
    #: Phase name -> default owner, derived while flattening.
    phase_defaults: dict[str, OwnerSpec] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _flatten_phases(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        raw_flow = data.get("flow")
        if not isinstance(raw_flow, list) or not raw_flow:
            return data

        phased = [
            item
            for item in raw_flow
            if isinstance(item, dict) and "phase" in item
        ]
        if not phased:
            return data
        if len(phased) != len(raw_flow):
            raise ValueError(
                "E_MIXED_FLOW_FORM: `flow` mixes a flat task list with phase "
                "sections; pick one form"
            )

        flat: list[dict[str, Any]] = []
        defaults: dict[str, Any] = {}
        for section in raw_flow:
            parsed = PhaseSection.model_validate(section)
            if parsed.default_owner is not None:
                defaults[parsed.phase] = parsed.default_owner
            for node in parsed.tasks:
                node.phase = parsed.phase
                flat.append(node)

        return {**data, "flow": flat, "phase_defaults": defaults}

    @model_validator(mode="after")
    def _number_nodes(self) -> Self:
        # Stable source ordering is what makes expansion deterministic.
        for index, node in enumerate(self.flow):
            node.source_index = index
        return self

    def param(self, name: str) -> ParamDef | None:
        return next((p for p in self.template_para if p.name == name), None)

    def role(self, name: str) -> RoleDef | None:
        return next((r for r in self.roles if r.name == name), None)

    def node(self, node_id: str) -> FlowNode | None:
        return next((n for n in self.flow if n.id == node_id), None)


# --- expanded output -------------------------------------------------------


class ExpandedTask(_Base):
    """A concrete task after the build-time pipeline has run."""

    id: str
    label: str
    uses: str = ""
    owner: str | None = None
    owner_source: str = "literal"
    group: str = ""
    duration_seconds: int = 0
    schedule_mode: ScheduleMode = ScheduleMode.CONTINUOUS
    calendar: str = "continuous"
    params: dict[str, Any] = Field(default_factory=dict)
    task_api: str = ""
    api_mode: ApiMode | None = None
    on_failure: FailurePolicy = FailurePolicy.BLOCK
    optional: bool = False
    phase: str = ""
    warn_before_seconds: int = 7200
    allow_manual_override: bool = True
    for_each_group: str | None = None
    for_each_index: int | None = None
    source_index: int = 0


class ExpandedEdge(_Base):
    predecessor: str
    successor: str
    lag_seconds: int = 0


class SkippedTask(_Base):
    id: str
    label: str
    reason: str


class ExpansionResult(_Base):
    """Output of the build-time pipeline (§4.15 steps 1-9)."""

    tasks: list[ExpandedTask] = Field(default_factory=list)
    edges: list[ExpandedEdge] = Field(default_factory=list)
    skipped: list[SkippedTask] = Field(default_factory=list)
    warnings: list[Issue] = Field(default_factory=list)
    buffer_seconds: int = 0

    def task(self, task_id: str) -> ExpandedTask | None:
        return next((t for t in self.tasks if t.id == task_id), None)

    def predecessors_of(self, task_id: str) -> list[ExpandedEdge]:
        return [e for e in self.edges if e.successor == task_id]

    def successors_of(self, task_id: str) -> list[ExpandedEdge]:
        return [e for e in self.edges if e.predecessor == task_id]


# --- helpers ---------------------------------------------------------------


def _reject_alias_conflicts(data: Any, aliases: dict[str, str]) -> Any:
    """Refuse a node that sets both a canonical name and its legacy alias.

    ``AliasChoices`` would quietly pick the first match, which is exactly the
    kind of silent divergence a template author cannot debug.
    """
    if not isinstance(data, dict):
        return data
    for canonical, alias in aliases.items():
        if canonical in data and alias in data:
            raise ValueError(
                f"E_ALIAS_CONFLICT: both `{canonical}` and its legacy alias "
                f"`{alias}` are set; keep only one"
            )
    return data


def _normalise_requirement(data: dict[str, Any]) -> list[Any]:
    """Accept every documented `requirement` shape (§4.3)."""
    value = data.get("requirement")
    if _is_blank_requirement(value):
        return []
    entries = value if isinstance(value, list) else [value]
    return [entry for entry in entries if not _is_blank_requirement(entry)]


#: pydantic error type -> domain code, for the codes we do not raise ourselves
_PYDANTIC_CODES = {
    "missing": "E_MISSING_FIELD",
    "extra_forbidden": "E_UNKNOWN_FIELD",
    "enum": "E_MISSING_FIELD",
    "literal_error": "E_MISSING_FIELD",
}


def issues_from_validation_error(
    exc: ValidationError, root: str = ""
) -> list[Issue]:
    """Translate pydantic errors into domain issues (design.md §9.2).

    Validators raise ``ValueError("E_SOME_CODE: message")``; that prefix is
    extracted here so domain rules keep their own codes while pydantic's
    built-in checks fall back to the table above.
    """
    issues: list[Issue] = []
    for raw in exc.errors():
        location = ".".join(str(part) for part in raw["loc"])
        path = (
            f"{root}.{location}" if root and location else (root or location)
        )
        message = raw["msg"].removeprefix("Value error, ")
        code = _PYDANTIC_CODES.get(raw["type"], "E_MISSING_FIELD")
        if ":" in message:
            candidate, _, rest = message.partition(":")
            if candidate.startswith(("E_", "W_")) and candidate.isupper():
                code, message = candidate, rest.strip()
        issues.append(Issue(code, message, Severity.ERROR, path))
    return issues
