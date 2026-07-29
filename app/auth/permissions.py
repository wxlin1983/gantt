"""Authorisation rules (implement.md §7.2).

Every function here is pure: it takes a :class:`Principal` plus the rows in
question and returns a bool. Group membership is resolved once per request and
carried on the principal, so these checks never issue queries and are directly
unit testable.

The API mirrors these results back to the client as a ``permissions`` object
so the UI can disable buttons, but that is presentation only — the server
re-checks every mutation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.models import CaseTask, GanttCase, User


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated user plus the groups they belong to."""

    user_id: int
    is_template_admin: bool = False
    is_active: bool = True
    group_ids: frozenset[int] = field(default_factory=frozenset)
    lead_group_ids: frozenset[int] = field(default_factory=frozenset)

    @classmethod
    def from_user(cls, user: User) -> Principal:
        """Build from a loaded user, using its ``memberships`` relationship."""
        memberships = user.memberships or []
        return cls(
            user_id=user.id,
            is_template_admin=user.is_template_admin,
            is_active=user.is_active,
            group_ids=frozenset(m.group_id for m in memberships),
            lead_group_ids=frozenset(
                m.group_id for m in memberships if m.is_lead
            ),
        )

    def in_group(self, group_id: int | None) -> bool:
        return group_id is not None and group_id in self.group_ids


# --- viewing ---------------------------------------------------------------


def can_view(principal: Principal) -> bool:
    """Any active user may read any case or template.

    Deliberately open: this is an internal coordination tool, and hiding
    flows from colleagues creates more problems than it solves. Every
    mutation is still gated, and reads are recorded nowhere.
    """
    return principal.is_active


# --- cases -----------------------------------------------------------------


def can_create_case(principal: Principal) -> bool:
    return principal.is_active


def can_edit_case(principal: Principal, case: GanttCase) -> bool:
    """Rename, change the target date, archive."""
    if not principal.is_active:
        return False
    return principal.is_template_admin or case.owner_id == principal.user_id


def can_cancel_case(principal: Principal, case: GanttCase) -> bool:
    return can_edit_case(principal, case)


def can_reassign_roles(principal: Principal, case: GanttCase) -> bool:
    return can_edit_case(principal, case)


def can_reset_baseline(principal: Principal, case: GanttCase) -> bool:
    """Overwriting the baseline erases what was originally promised.

    Restricted to the same people who can move the target date, and the UI
    asks for explicit confirmation on top.
    """
    return can_edit_case(principal, case)


# --- tasks -----------------------------------------------------------------


def can_complete_task(
    principal: Principal, task: CaseTask, case: GanttCase | None = None
) -> bool:
    """Mark a task finished.

    Group members may act for each other on purpose: when the named owner is
    on leave the flow must not stall. The audit trail records who actually
    did it via ``completed_by_id``.
    """
    if not principal.is_active:
        return False
    if task.owner_id == principal.user_id:
        return True
    if principal.in_group(task.group_id):
        return True
    # A case owner can unblock their own flow even outside the group.
    return case is not None and case.owner_id == principal.user_id


def can_edit_task(
    principal: Principal, task: CaseTask, case: GanttCase | None = None
) -> bool:
    """Change duration, parameters, owner or dependencies."""
    if not principal.is_active:
        return False
    if principal.is_template_admin:
        return True
    return can_complete_task(principal, task, case)


def can_retry_task(
    principal: Principal, task: CaseTask, case: GanttCase | None = None
) -> bool:
    return can_complete_task(principal, task, case)


def can_insert_task(
    principal: Principal,
    case: GanttCase,
    neighbours: list[CaseTask] | None = None,
) -> bool:
    """Insert a task, optionally between existing ones.

    Anyone who could edit one of the adjacent tasks may also insert next to
    it, which is what makes "we forgot a step" fixable by the people doing
    the work rather than only by the case owner.
    """
    if not principal.is_active:
        return False
    if principal.is_template_admin or case.owner_id == principal.user_id:
        return True
    return any(
        can_edit_task(principal, task, case) for task in neighbours or []
    )


def can_delete_task(
    principal: Principal, task: CaseTask, case: GanttCase
) -> bool:
    return can_edit_task(principal, task, case)


def can_reopen_task(principal: Principal) -> bool:
    """Undo a completion. Admin only: `done` is otherwise a terminal state."""
    return principal.is_active and principal.is_template_admin


# --- templates and administration -----------------------------------------


def can_manage_templates(principal: Principal) -> bool:
    """Create, edit, publish, import gantt and task templates."""
    return principal.is_active and principal.is_template_admin


def can_manage_calendars(principal: Principal) -> bool:
    return can_manage_templates(principal)


def can_manage_credentials(principal: Principal) -> bool:
    return can_manage_templates(principal)


def can_manage_users(principal: Principal) -> bool:
    return can_manage_templates(principal)


# --- serialisation ---------------------------------------------------------


def case_permissions(principal: Principal, case: GanttCase) -> dict[str, bool]:
    """Permission flags sent alongside a case payload (§7.3)."""
    return {
        "can_edit": can_edit_case(principal, case),
        "can_cancel": can_cancel_case(principal, case),
        "can_reassign_roles": can_reassign_roles(principal, case),
        "can_reset_baseline": can_reset_baseline(principal, case),
        "can_insert_task": can_insert_task(principal, case),
    }


def task_permissions(
    principal: Principal, task: CaseTask, case: GanttCase | None = None
) -> dict[str, bool]:
    """Permission flags sent alongside a task payload (§7.3)."""
    return {
        "can_edit": can_edit_task(principal, task, case),
        "can_complete": can_complete_task(principal, task, case),
        "can_retry": can_retry_task(principal, task, case),
        "can_reopen": can_reopen_task(principal),
    }
