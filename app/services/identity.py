"""Resolving the names a template uses into user and group rows.

Templates name people and groups as strings; the database keys on ids. Nothing
here refuses to resolve: a template that names someone who has since left
should still produce a case, with the task showing as unassigned rather than
the whole creation failing.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Group, GroupMember, User


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
