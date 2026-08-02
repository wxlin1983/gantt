"""Resolving the names a template uses into user and group rows.

Templates name people and groups as strings; the database keys on ids. Nothing
here refuses to resolve: a template that names someone who has since left
should still produce a case, with the task showing as unassigned rather than
the whole creation failing.
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth.passwords import hash_password
from app.models import CaseTask, Group, GroupMember, User


async def users_by_name(
    session: AsyncSession, usernames: set[str]
) -> dict[str, User]:
    if not usernames:
        return {}
    rows = (
        await session.scalars(
            select(User).where(User.username.in_(usernames))
        )
    ).all()
    return {row.username: row for row in rows}


async def groups_by_name(
    session: AsyncSession, names: set[str]
) -> dict[str, Group]:
    if not names:
        return {}
    rows = (
        await session.scalars(select(Group).where(Group.name.in_(names)))
    ).all()
    return {row.name: row for row in rows}


async def group_leads(
    session: AsyncSession, names: set[str]
) -> dict[str, int]:
    """Map group name to its lead's user id.

    A group with several leads resolves to the lowest user id -- deterministic
    rather than arbitrary. The DSL validator warns about it separately.
    """
    if not names:
        return {}
    rows = (
        await session.execute(
            select(Group.name, GroupMember.user_id)
            .join(GroupMember, GroupMember.group_id == Group.id)
            .where(Group.name.in_(names), GroupMember.is_lead.is_(True))
            .order_by(Group.name, GroupMember.user_id)
        )
    ).all()
    leads: dict[str, int] = {}
    for name, user_id in rows:
        leads.setdefault(name, user_id)
    return leads


async def user_ids_in_group(
    session: AsyncSession, group_id: int
) -> set[int]:
    rows = await session.scalars(
        select(GroupMember.user_id).where(GroupMember.group_id == group_id)
    )
    return set(rows.all())


# --- administration --------------------------------------------------------
#
# Everything above resolves names a template already used. Everything below
# maintains the directory those names come from, which until now existed only
# in `gantt seed` -- so the only way to add a colleague was to open a shell,
# and a case could be assigned to a username nobody had ever created.


class IdentityError(Exception):
    """A directory change could not be applied."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


async def list_users(
    session: AsyncSession, include_inactive: bool = True
) -> list[User]:
    statement = (
        select(User)
        .options(selectinload(User.memberships))
        .order_by(User.username)
    )
    if not include_inactive:
        statement = statement.where(User.is_active.is_(True))
    return list((await session.scalars(statement)).unique().all())


async def get_user(session: AsyncSession, user_id: int) -> User:
    user = (
        await session.scalars(
            select(User)
            .options(selectinload(User.memberships))
            .where(User.id == user_id)
        )
    ).one_or_none()
    if user is None:
        raise IdentityError("E_USER_NOT_FOUND", f"no user {user_id}")
    return user


async def create_user(
    session: AsyncSession,
    *,
    username: str,
    display_name: str = "",
    email: str = "",
    password: str | None = None,
    is_template_admin: bool = False,
) -> User:
    """Add a person to the directory.

    Username and email are checked here rather than left to the unique
    constraint: a violated constraint surfaces as an opaque database error
    halfway through a transaction, and the caller cannot tell which field was
    at fault.
    """
    username = username.strip()
    if not username:
        raise IdentityError("E_BAD_USERNAME", "username must not be empty")

    clash = (
        await session.scalars(
            select(User).where(User.username == username)
        )
    ).one_or_none()
    if clash is not None:
        raise IdentityError(
            "E_DUPLICATE_USER", f"`{username}` is already taken"
        )
    if email:
        by_email = (
            await session.scalars(select(User).where(User.email == email))
        ).one_or_none()
        if by_email is not None:
            raise IdentityError(
                "E_DUPLICATE_EMAIL", f"`{email}` is already in use"
            )

    user = User(
        username=username,
        display_name=display_name or username,
        # Blank becomes NULL: the unique constraint treats every empty string
        # as the same value, so two accounts without an email would collide.
        email=email or None,
        password_hash=hash_password(password) if password else None,
        is_active=True,
        is_template_admin=is_template_admin,
    )
    session.add(user)
    await session.flush()
    await session.refresh(user, ["memberships"])
    return user


async def update_user(
    session: AsyncSession,
    user: User,
    *,
    display_name: str | None = None,
    email: str | None = None,
    is_template_admin: bool | None = None,
    is_active: bool | None = None,
) -> User:
    if display_name is not None:
        user.display_name = display_name
    if email is not None and (email or None) != user.email:
        clash = (
            await session.scalars(
                select(User).where(User.email == email, User.id != user.id)
            )
        ).one_or_none()
        if clash is not None:
            raise IdentityError(
                "E_DUPLICATE_EMAIL", f"`{email}` is already in use"
            )
        user.email = email or None
    if is_template_admin is not None:
        user.is_template_admin = is_template_admin
    if is_active is not None:
        user.is_active = is_active
    await session.flush()
    return user


async def set_password(
    session: AsyncSession, user: User, password: str
) -> None:
    user.password_hash = hash_password(password)
    await session.flush()


async def list_groups(session: AsyncSession) -> list[Group]:
    return list(
        (
            await session.scalars(
                select(Group)
                .options(selectinload(Group.members))
                .order_by(Group.name)
            )
        )
        .unique()
        .all()
    )


async def get_group(session: AsyncSession, group_id: int) -> Group:
    group = (
        await session.scalars(
            select(Group)
            .options(selectinload(Group.members))
            .where(Group.id == group_id)
        )
    ).one_or_none()
    if group is None:
        raise IdentityError("E_GROUP_NOT_FOUND", f"no group {group_id}")
    return group


async def create_group(
    session: AsyncSession, *, name: str, display_name: str = ""
) -> Group:
    name = name.strip()
    if not name:
        raise IdentityError("E_BAD_GROUP_NAME", "name must not be empty")
    clash = (
        await session.scalars(select(Group).where(Group.name == name))
    ).one_or_none()
    if clash is not None:
        raise IdentityError(
            "E_DUPLICATE_GROUP", f"`{name}` already exists"
        )
    group = Group(name=name, display_name=display_name or name)
    session.add(group)
    await session.flush()
    await session.refresh(group, ["members"])
    return group


async def update_group(
    session: AsyncSession, group: Group, *, display_name: str | None = None
) -> Group:
    # `name` is deliberately not editable: templates refer to groups by it,
    # and a rename would silently detach every task that names the old one.
    if display_name is not None:
        group.display_name = display_name
    await session.flush()
    return group


async def set_members(
    session: AsyncSession,
    group: Group,
    members: list[tuple[int, bool]],
) -> Group:
    """Replace the membership list wholesale.

    A whole list rather than add/remove calls: the editor shows the group as a
    set of checkboxes and saves once, so a partial application would be a
    state the user never asked for.
    """
    wanted = {user_id: is_lead for user_id, is_lead in members}
    if wanted:
        found = set(
            (
                await session.scalars(
                    select(User.id).where(User.id.in_(wanted))
                )
            ).all()
        )
        missing = sorted(set(wanted) - found)
        if missing:
            raise IdentityError(
                "E_USER_NOT_FOUND",
                "no such user: " + ", ".join(str(i) for i in missing),
            )

    await session.execute(
        delete(GroupMember).where(GroupMember.group_id == group.id)
    )
    for user_id, is_lead in wanted.items():
        session.add(
            GroupMember(
                group_id=group.id, user_id=user_id, is_lead=is_lead
            )
        )
    await session.flush()
    await session.refresh(group, ["members"])
    return group


async def delete_group(session: AsyncSession, group: Group) -> None:
    """Remove a group nothing refers to.

    Refused while any task points at it. Deleting would either orphan those
    rows or cascade away a case's record of who was responsible, and neither
    is a thing an administrator meant to do by clicking `delete`.
    """
    in_use = (
        await session.scalars(
            select(CaseTask.id).where(CaseTask.group_id == group.id).limit(1)
        )
    ).one_or_none()
    if in_use is not None:
        raise IdentityError(
            "E_GROUP_IN_USE",
            f"`{group.name}` is still assigned to tasks",
        )
    await session.delete(group)
    await session.flush()
