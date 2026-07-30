"""Session authentication (implement.md §7.1)."""

from __future__ import annotations

from fastapi import APIRouter, Request, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.auth.passwords import hash_password, needs_rehash, verify_password
from app.models import Group, GroupMember, User

from ..deps import SESSION_USER_KEY, SessionDep, UserDep
from ..errors import ApiError
from ..schemas import LoginRequest, MeOut, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=MeOut)
async def login(
    request: Request, body: LoginRequest, session: SessionDep
) -> MeOut:
    """Exchange credentials for a session cookie."""
    user = (
        await session.scalars(
            select(User)
            .where(User.username == body.username)
            .options(selectinload(User.memberships))
        )
    ).one_or_none()

    # verify_password runs against a dummy hash for a missing account, so the
    # response time does not reveal whether the username exists.
    stored = user.password_hash if user else None
    if not verify_password(body.password, stored):
        raise ApiError(
            "E_BAD_CREDENTIALS",
            "username or password is incorrect",
            http_status=status.HTTP_401_UNAUTHORIZED,
        )
    if not user.is_active:
        raise ApiError(
            "E_ACCOUNT_DISABLED",
            "this account is disabled",
            http_status=status.HTTP_403_FORBIDDEN,
        )

    if needs_rehash(user.password_hash):
        # Upgrade silently on a successful login rather than forcing a reset.
        user.password_hash = hash_password(body.password)
        await session.flush()

    request.session[SESSION_USER_KEY] = user.id
    return await _describe(session, user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request) -> None:
    request.session.clear()


@router.get("/me", response_model=MeOut)
async def me(session: SessionDep, user: UserDep) -> MeOut:
    return await _describe(session, user)


async def _describe(session: SessionDep, user: User) -> MeOut:
    rows = (
        await session.execute(
            select(Group.name, GroupMember.is_lead)
            .join(GroupMember, GroupMember.group_id == Group.id)
            .where(GroupMember.user_id == user.id)
            .order_by(Group.name)
        )
    ).all()
    return MeOut(
        user=UserOut.model_validate(user),
        groups=[name for name, _ in rows],
        lead_of=[name for name, is_lead in rows if is_lead],
    )
