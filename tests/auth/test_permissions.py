"""Authorisation matrix (implement.md §7.2).

Each rule is checked from both sides: someone who should be allowed, and
someone who should not.
"""

from __future__ import annotations

import pytest

from app.auth.permissions import (
    Principal,
    can_complete_task,
    can_edit_case,
    can_edit_task,
    can_insert_task,
    can_manage_templates,
    can_reopen_task,
    can_view,
    case_permissions,
    task_permissions,
)
from app.models import CaseTask, GanttCase

OWNER = 1
GROUP_MEMBER = 2
STRANGER = 3
CASE_OWNER = 4
ADMIN = 5

QA_GROUP = 10
OTHER_GROUP = 11


def principal(user_id: int, *, admin: bool = False, groups=()) -> Principal:
    return Principal(
        user_id=user_id,
        is_template_admin=admin,
        group_ids=frozenset(groups),
    )


@pytest.fixture
def case() -> GanttCase:
    return GanttCase(id=1, name="c", owner_id=CASE_OWNER)


@pytest.fixture
def task() -> CaseTask:
    return CaseTask(id=1, name="t", owner_id=OWNER, group_id=QA_GROUP)


class TestViewing:
    def test_any_active_user_may_read(self):
        assert can_view(principal(STRANGER))

    def test_deactivated_user_may_not(self):
        assert not can_view(Principal(user_id=STRANGER, is_active=False))


class TestCaseEditing:
    def test_case_owner_may_edit(self, case):
        assert can_edit_case(principal(CASE_OWNER), case)

    def test_admin_may_edit(self, case):
        assert can_edit_case(principal(STRANGER, admin=True), case)

    def test_stranger_may_not_edit(self, case):
        assert not can_edit_case(principal(STRANGER), case)

    def test_task_owner_may_not_edit_the_case(self, case):
        assert not can_edit_case(principal(OWNER), case)


class TestTaskCompletion:
    def test_task_owner_may_complete(self, task, case):
        assert can_complete_task(principal(OWNER), task, case)

    def test_group_member_may_complete(self, task, case):
        # Covering for an absent colleague must not require an admin
        assert can_complete_task(
            principal(GROUP_MEMBER, groups=[QA_GROUP]), task, case
        )

    def test_case_owner_may_complete(self, task, case):
        assert can_complete_task(principal(CASE_OWNER), task, case)

    def test_other_group_may_not_complete(self, task, case):
        assert not can_complete_task(
            principal(GROUP_MEMBER, groups=[OTHER_GROUP]), task, case
        )

    def test_stranger_may_not_complete(self, task, case):
        assert not can_complete_task(principal(STRANGER), task, case)

    def test_admin_is_not_automatically_a_completer(self, task, case):
        # Template admin governs template editing, not doing other people's
        # work; completing still needs a real relationship to the task.
        assert not can_complete_task(principal(ADMIN, admin=True), task, case)

    def test_ungrouped_task_falls_back_to_owner_only(self, case):
        task = CaseTask(id=2, name="t2", owner_id=OWNER, group_id=None)
        assert can_complete_task(principal(OWNER), task, case)
        assert not can_complete_task(
            principal(GROUP_MEMBER, groups=[QA_GROUP]), task, case
        )


class TestTaskEditing:
    def test_admin_may_edit_any_task(self, task, case):
        assert can_edit_task(principal(ADMIN, admin=True), task, case)

    def test_group_member_may_edit(self, task, case):
        assert can_edit_task(
            principal(GROUP_MEMBER, groups=[QA_GROUP]), task, case
        )

    def test_stranger_may_not_edit(self, task, case):
        assert not can_edit_task(principal(STRANGER), task, case)


class TestInsertion:
    def test_case_owner_may_insert(self, case):
        assert can_insert_task(principal(CASE_OWNER), case)

    def test_neighbour_group_member_may_insert(self, case, task):
        assert can_insert_task(
            principal(GROUP_MEMBER, groups=[QA_GROUP]), case, [task]
        )

    def test_stranger_may_not_insert(self, case, task):
        assert not can_insert_task(principal(STRANGER), case, [task])

    def test_stranger_may_not_insert_without_neighbours(self, case):
        assert not can_insert_task(principal(STRANGER), case)


class TestReopen:
    def test_only_admin_may_reopen(self):
        assert can_reopen_task(principal(ADMIN, admin=True))
        assert not can_reopen_task(principal(CASE_OWNER))
        assert not can_reopen_task(principal(OWNER))


class TestTemplateAdministration:
    def test_admin_only(self):
        assert can_manage_templates(principal(ADMIN, admin=True))
        assert not can_manage_templates(principal(CASE_OWNER))

    def test_deactivated_admin_loses_access(self):
        deactivated = Principal(
            user_id=ADMIN, is_template_admin=True, is_active=False
        )
        assert not can_manage_templates(deactivated)


class TestSerialisation:
    def test_case_flags(self, case):
        flags = case_permissions(principal(CASE_OWNER), case)
        assert flags == {
            "can_edit": True,
            "can_cancel": True,
            "can_reassign_roles": True,
            "can_reset_baseline": True,
            "can_insert_task": True,
        }

    def test_task_flags_for_a_stranger(self, task, case):
        flags = task_permissions(principal(STRANGER), task, case)
        assert not any(flags.values())


class TestPrincipalFromUser:
    def test_collects_groups_and_leadership(self):
        from app.models import GroupMember, User

        user = User(
            id=OWNER,
            username="u",
            display_name="U",
            email="u@example.com",
            is_active=True,
            is_template_admin=False,
        )
        user.memberships = [
            GroupMember(group_id=QA_GROUP, user_id=OWNER, is_lead=True),
            GroupMember(group_id=OTHER_GROUP, user_id=OWNER, is_lead=False),
        ]
        result = Principal.from_user(user)
        assert result.group_ids == frozenset({QA_GROUP, OTHER_GROUP})
        assert result.lead_group_ids == frozenset({QA_GROUP})
