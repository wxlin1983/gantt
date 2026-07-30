"""Request-scoped dependencies: session, current user, principal."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth.permissions import Principal
from app.db import session_scope
from app.models import User

from .errors import ApiError

SESSION_USER_KEY = "user_id"


async def get_session() -> AsyncIterator[AsyncSession]:
    async with session_scope() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def current_user(
    request: Request, session: SessionDep
) -> User:
    """The signed-in user, with group memberships loaded.

    Memberships are eager-loaded because almost every authorisation check
    needs them, and doing it once per request beats a lazy load per task.
    """
    user_id = request.session.get(SESSION_USER_KEY)
    if not user_id:
        raise ApiError("E_UNAUTHENTICATED", "sign in first")

    user = (
        await session.scalars(
            select(User)
            .where(User.id == user_id)
            .options(selectinload(User.memberships))
        )
    ).one_or_none()
    if user is None or not user.is_active:
        # The account was deleted or deactivated while the cookie lived on.
        request.session.clear()
        raise ApiError("E_UNAUTHENTICATED", "sign in again")
    return user


UserDep = Annotated[User, Depends(current_user)]


async def current_principal(user: UserDep) -> Principal:
    return Principal.from_user(user)


PrincipalDep = Annotated[Principal, Depends(current_principal)]


def require(allowed: bool, message: str) -> None:
    """Guard for an authorisation check.

    Deliberately reports the same code for every refusal so the API does not
    become a map of what exists and who owns it.
    """
    if not allowed:
        raise ApiError("E_FORBIDDEN", message)
