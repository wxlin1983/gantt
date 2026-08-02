"""Users and groups (implement.md §7.1).

The directory existed only in `gantt seed` and the database: there was no way
to add a colleague without a shell, and no way for the case wizard to offer a
list of real people to assign a role to. That is how a case came to be created
against a username nobody had ever registered.

Every route here is administrator-only. Reading the directory is not -- the
wizard and the task drawer need to name people, and hiding who your colleagues
are from an internal coordination tool solves nothing.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from app.auth import permissions
from app.services import identity as directory

from ..deps import PrincipalDep, SessionDep, UserDep, require
from ..errors import ApiError
from ..schemas import (
    CreateGroupRequest,
    CreateUserRequest,
    GroupOut,
    PersonOut,
    SetMembersRequest,
    SetPasswordRequest,
    UpdateGroupRequest,
    UpdateUserRequest,
)

router = APIRouter(tags=["directory"])


def _person(user) -> PersonOut:
    return PersonOut(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        email=user.email or "",
        is_active=user.is_active,
        is_template_admin=user.is_template_admin,
        has_password=bool(user.password_hash),
        memberships=[
            {"group_id": m.group_id, "is_lead": m.is_lead}
            for m in user.memberships
        ],
    )


def _group(group, names: dict[int, tuple[str, str]]) -> GroupOut:
    return GroupOut(
        id=group.id,
        name=group.name,
        display_name=group.display_name,
        members=[
            {
                "user_id": m.user_id,
                "username": names.get(m.user_id, ("", ""))[0],
                "display_name": names.get(m.user_id, ("", ""))[1],
                "is_lead": m.is_lead,
            }
            for m in group.members
        ],
    )


# --- users -----------------------------------------------------------------


@router.get("/users", response_model=list[PersonOut])
async def list_users(
    session: SessionDep, principal: PrincipalDep
) -> list[PersonOut]:
    # Readable by anyone signed in: the wizard needs it to offer a role a real
    # person, which is the whole reason this router exists.
    require(permissions.can_view(principal), "sign in first")
    return [_person(user) for user in await directory.list_users(session)]


@router.post(
    "/users", response_model=PersonOut, status_code=status.HTTP_201_CREATED
)
async def create_user(
    body: CreateUserRequest, session: SessionDep, principal: PrincipalDep
) -> PersonOut:
    require(
        permissions.can_manage_people(principal),
        "only an administrator can add users",
    )
    try:
        user = await directory.create_user(
            session,
            username=body.username,
            display_name=body.display_name,
            email=body.email,
            password=body.password,
            is_template_admin=body.is_template_admin,
        )
    except directory.IdentityError as exc:
        raise ApiError(exc.code, str(exc)) from exc
    return _person(user)


@router.patch("/users/{user_id}", response_model=PersonOut)
async def update_user(
    user_id: int,
    body: UpdateUserRequest,
    session: SessionDep,
    principal: PrincipalDep,
    actor: UserDep,
) -> PersonOut:
    require(
        permissions.can_manage_people(principal),
        "only an administrator can edit users",
    )
    try:
        user = await directory.get_user(session, user_id)
        # Locking yourself out is a one-click mistake with no way back that
        # does not involve a shell, so the two fields that could do it are
        # refused on your own account.
        if user.id == actor.id:
            if body.is_active is False:
                raise ApiError(
                    "E_SELF_LOCKOUT", "you cannot deactivate your own account"
                )
            if body.is_template_admin is False:
                raise ApiError(
                    "E_SELF_LOCKOUT",
                    "you cannot remove your own administrator rights",
                )
        user = await directory.update_user(
            session,
            user,
            display_name=body.display_name,
            email=body.email,
            is_template_admin=body.is_template_admin,
            is_active=body.is_active,
        )
    except directory.IdentityError as exc:
        raise ApiError(exc.code, str(exc)) from exc
    return _person(user)


@router.put(
    "/users/{user_id}/password",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def set_password(
    user_id: int,
    body: SetPasswordRequest,
    session: SessionDep,
    principal: PrincipalDep,
) -> None:
    require(
        permissions.can_manage_people(principal),
        "only an administrator can set passwords",
    )
    try:
        user = await directory.get_user(session, user_id)
        await directory.set_password(session, user, body.password)
    except directory.IdentityError as exc:
        raise ApiError(exc.code, str(exc)) from exc


# --- groups ----------------------------------------------------------------


async def _member_names(session) -> dict[int, tuple[str, str]]:
    return {
        user.id: (user.username, user.display_name)
        for user in await directory.list_users(session)
    }


@router.get("/groups", response_model=list[GroupOut])
async def list_groups(
    session: SessionDep, principal: PrincipalDep
) -> list[GroupOut]:
    require(permissions.can_view(principal), "sign in first")
    names = await _member_names(session)
    return [
        _group(group, names) for group in await directory.list_groups(session)
    ]


@router.post(
    "/groups", response_model=GroupOut, status_code=status.HTTP_201_CREATED
)
async def create_group(
    body: CreateGroupRequest, session: SessionDep, principal: PrincipalDep
) -> GroupOut:
    require(
        permissions.can_manage_people(principal),
        "only an administrator can add groups",
    )
    try:
        group = await directory.create_group(
            session, name=body.name, display_name=body.display_name
        )
    except directory.IdentityError as exc:
        raise ApiError(exc.code, str(exc)) from exc
    return _group(group, await _member_names(session))


@router.patch("/groups/{group_id}", response_model=GroupOut)
async def update_group(
    group_id: int,
    body: UpdateGroupRequest,
    session: SessionDep,
    principal: PrincipalDep,
) -> GroupOut:
    require(
        permissions.can_manage_people(principal),
        "only an administrator can edit groups",
    )
    try:
        group = await directory.get_group(session, group_id)
        group = await directory.update_group(
            session, group, display_name=body.display_name
        )
    except directory.IdentityError as exc:
        raise ApiError(exc.code, str(exc)) from exc
    return _group(group, await _member_names(session))


@router.put("/groups/{group_id}/members", response_model=GroupOut)
async def set_members(
    group_id: int,
    body: SetMembersRequest,
    session: SessionDep,
    principal: PrincipalDep,
) -> GroupOut:
    require(
        permissions.can_manage_people(principal),
        "only an administrator can change membership",
    )
    try:
        group = await directory.get_group(session, group_id)
        group = await directory.set_members(
            session,
            group,
            [(m.user_id, m.is_lead) for m in body.members],
        )
    except directory.IdentityError as exc:
        raise ApiError(exc.code, str(exc)) from exc
    return _group(group, await _member_names(session))


@router.delete(
    "/groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_group(
    group_id: int, session: SessionDep, principal: PrincipalDep
) -> None:
    require(
        permissions.can_manage_people(principal),
        "only an administrator can delete groups",
    )
    try:
        group = await directory.get_group(session, group_id)
        await directory.delete_group(session, group)
    except directory.IdentityError as exc:
        raise ApiError(exc.code, str(exc)) from exc
