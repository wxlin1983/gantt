"""Request and response bodies."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models import (
    CaseHealth,
    CaseStatus,
    CompletionSource,
    TaskStatus,
)


class Body(BaseModel):
    # Reject unknown keys so a client typo surfaces immediately rather than
    # being silently dropped.
    model_config = ConfigDict(extra="forbid")


class Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- auth ------------------------------------------------------------------


class LoginRequest(Body):
    username: str
    password: str


class UserOut(Out):
    id: int
    username: str
    display_name: str
    #: Optional: an internal account need not have a mailbox.
    email: str | None = None
    is_template_admin: bool


class MeOut(Out):
    user: UserOut
    groups: list[str] = Field(default_factory=list)
    lead_of: list[str] = Field(default_factory=list)


# --- cases -----------------------------------------------------------------


class CreateCaseRequest(Body):
    name: str = Field(min_length=1, max_length=256)
    template_name: str
    target_date: datetime
    template_version: int | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    role_assignments: dict[str, str] = Field(default_factory=dict)
    #: Client-generated once per wizard session, so a double submit returns
    #: the case already created instead of a second one (§8.2).
    idempotency_key: str | None = Field(default=None, max_length=128)


class PreviewRequest(Body):
    template_name: str
    target_date: datetime
    template_version: int | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    role_assignments: dict[str, str] = Field(default_factory=dict)
    name: str = "preview"


class UpdateCaseRequest(Body):
    name: str | None = Field(default=None, min_length=1, max_length=256)
    target_date: datetime | None = None
    note: str = ""


class UpdateTaskRequest(Body):
    duration_seconds: int | None = Field(default=None, ge=0)
    owner_id: int | None = None
    group_id: int | None = None
    display_name: str | None = None
    params: dict[str, Any] | None = None
    expected_version: int | None = None


class CompleteTaskRequest(Body):
    at: datetime | None = None
    note: str = ""


class CancelRequest(Body):
    note: str = ""


class TaskOut(Out):
    id: int
    name: str
    display_name: str
    phase: str
    status: TaskStatus
    owner_id: int | None
    owner_source: str
    group_id: int | None
    duration_seconds: int
    baseline_start: datetime | None
    baseline_end: datetime | None
    forecast_start: datetime | None
    forecast_end: datetime | None
    actual_start: datetime | None
    actual_end: datetime | None
    is_on_critical_path: bool
    is_optional: bool
    #: True for tasks added after creation, which have no baseline to compare
    #: against and are drawn as a single bar (§5.10).
    is_unplanned: bool
    task_api: str | None
    completion_source: CompletionSource | None
    completion_note: str
    version: int
    permissions: dict[str, bool] = Field(default_factory=dict)


class EdgeOut(BaseModel):
    predecessor: str
    successor: str
    lag_seconds: int


class SkippedOut(BaseModel):
    id: str
    label: str
    reason: str


class CaseSummaryOut(Out):
    id: int
    name: str
    template_name: str
    template_version: int
    status: CaseStatus
    health: CaseHealth | None
    target_date: datetime
    forecast_end: datetime | None
    progress_ratio: float | None
    buffer_consumed_ratio: float | None
    owner_id: int | None
    #: Resolved on the row so the list can name a person rather than print an
    #: id; empty when the case has no owner.
    owner_name: str = ""
    created_at: datetime
    #: Which task the case is waiting on, so the list answers "stuck where?"
    #: without opening the case (design.md §8.2).
    blocked_on: list[str] = Field(default_factory=list)
    exceeds_target_by_seconds: int = 0


class CaseDetailOut(CaseSummaryOut):
    params: dict[str, Any] = Field(default_factory=dict)
    role_assignments: dict[str, Any] = Field(default_factory=dict)
    buffer_seconds: int = 0
    target_date_history: list[Any] = Field(default_factory=list)
    skipped_tasks: list[Any] = Field(default_factory=list)
    tasks: list[TaskOut] = Field(default_factory=list)
    dependencies: list[EdgeOut] = Field(default_factory=list)
    permissions: dict[str, bool] = Field(default_factory=dict)
    version: int = 1
    #: user id -> display name, for every owner referenced by this case.
    #: Sent once per case rather than repeated on each task, and it is what
    #: lets the UI show a person instead of a number.
    people: dict[str, str] = Field(default_factory=dict)
    #: group id -> name, same reasoning.
    groups: dict[str, str] = Field(default_factory=dict)


class PreviewTaskOut(BaseModel):
    name: str
    display_name: str
    phase: str
    owner: str | None
    group: str
    duration_seconds: int
    baseline_start: datetime
    baseline_end: datetime
    is_on_critical_path: bool
    is_optional: bool


class PreviewOut(BaseModel):
    """Result of a dry run (§8.3).

    Shared by the creation wizard's feasibility step and the template editor's
    trial run, which is why it reports both the schedule and its viability.
    """

    tasks: list[PreviewTaskOut]
    dependencies: list[EdgeOut]
    skipped_tasks: list[SkippedOut]
    earliest_start: datetime
    plan_deadline: datetime
    target_date: datetime
    buffer_seconds: int
    critical_path_seconds: int
    critical_path: list[str]
    feasible: bool
    slack_seconds: int
    warnings: list[dict[str, Any]] = Field(default_factory=list)


class HealthCountsOut(BaseModel):
    on_track: int = 0
    at_risk: int = 0
    overdue: int = 0
    completed: int = 0
    cancelled: int = 0


class MyTaskOut(Out):
    case_id: int
    case_name: str
    task_id: int
    name: str
    display_name: str
    status: TaskStatus
    baseline_start: datetime | None
    baseline_end: datetime | None
    forecast_end: datetime | None
    is_late_start: bool


class InsertTaskRequest(Body):
    name: str = Field(min_length=1, max_length=128)
    display_name: str = ""
    task_template: str | None = None
    duration_seconds: int | None = Field(default=None, ge=0)
    owner_id: int | None = None
    group_id: int | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    predecessors: list[str] = Field(default_factory=list)
    successors: list[str] = Field(default_factory=list)
    #: `serial` splices into the chain; `parallel` hangs alongside it. Spelled
    #: out rather than inferred, because it is the choice people get wrong.
    mode: Literal["serial", "parallel"] = "serial"


class DeleteTaskRequest(Body):
    mode: Literal["reconnect", "detach"] = "reconnect"


class SimulateRequest(Body):
    task_name: str | None = None
    duration_seconds: int | None = Field(default=None, ge=0)
    insert_after: str | None = None
    insert_duration_seconds: int = Field(default=0, ge=0)


class SimulateOut(BaseModel):
    current_forecast_end: datetime | None
    simulated_forecast_end: datetime | None
    delta_seconds: int
    affected: list[dict[str, Any]]
    exceeds_target: bool
    exceeds_target_by_seconds: int


class ResetBaselineRequest(Body):
    note: str = ""


class TaskRunOut(Out):
    attempt: int
    handler_name: str
    status: str
    external_ref: str | None
    error_message: str | None
    error_detail: str | None
    started_at: datetime
    finished_at: datetime | None


class NotificationOut(Out):
    id: int
    type: str
    title: str
    body: str
    case_id: int | None
    case_task_id: int | None
    read_at: datetime | None
    created_at: datetime


class AuditEventOut(Out):
    id: int
    event_type: str
    actor_id: int | None
    case_task_id: int | None
    note: str
    created_at: datetime
    before_state: dict[str, Any] | None
    after_state: dict[str, Any] | None


# --- directory -------------------------------------------------------------


class GroupMembershipOut(BaseModel):
    group_id: int
    is_lead: bool


class PersonOut(Out):
    id: int
    username: str
    display_name: str
    email: str | None = None
    is_active: bool
    is_template_admin: bool
    #: Whether a local password is set. The hash itself never leaves the
    #: server, but "can this person sign in at all" is worth showing.
    has_password: bool = False
    memberships: list[GroupMembershipOut] = Field(default_factory=list)


class CreateUserRequest(Body):
    username: str = Field(min_length=1, max_length=64)
    display_name: str = ""
    email: str = Field(default="", max_length=256)
    password: str | None = Field(default=None, min_length=8)
    is_template_admin: bool = False


class UpdateUserRequest(Body):
    display_name: str | None = Field(default=None, max_length=128)
    email: str | None = Field(default=None, max_length=256)
    is_template_admin: bool | None = None
    is_active: bool | None = None


class SetPasswordRequest(Body):
    password: str = Field(min_length=8)


class GroupMemberOut(BaseModel):
    user_id: int
    username: str
    display_name: str
    is_lead: bool


class GroupOut(BaseModel):
    id: int
    name: str
    display_name: str
    members: list[GroupMemberOut] = Field(default_factory=list)


class CreateGroupRequest(Body):
    name: str = Field(min_length=1, max_length=64)
    display_name: str = ""


class UpdateGroupRequest(Body):
    display_name: str | None = Field(default=None, max_length=128)


class MemberRequest(Body):
    user_id: int
    is_lead: bool = False


class SetMembersRequest(Body):
    members: list[MemberRequest] = Field(default_factory=list)


# --- calendars -------------------------------------------------------------


class CalendarOut(Out):
    id: int
    name: str
    timezone: str
    #: {"mon": [["09:00", "18:00"]], ...}
    working_hours: dict[str, Any] = Field(default_factory=dict)
    holidays: list[str] = Field(default_factory=list)
    is_builtin: bool = False
    #: What `1D` converts to on this calendar (§4.5), which is worth seeing
    #: because that conversion has been a real source of error.
    day_seconds: int = 0
    #: `continuous` means 24x7 and the engine ignores its row entirely.
    is_editable: bool = True


class CreateCalendarRequest(Body):
    name: str = Field(min_length=1, max_length=64)
    timezone: str = Field(default="Asia/Taipei", max_length=64)
    working_hours: dict[str, Any] = Field(default_factory=dict)
    holidays: list[str] = Field(default_factory=list)


class UpdateCalendarRequest(Body):
    timezone: str | None = Field(default=None, max_length=64)
    working_hours: dict[str, Any] | None = None
    holidays: list[str] | None = None
