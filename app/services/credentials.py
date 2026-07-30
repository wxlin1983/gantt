"""Secret storage for handler configuration (implement.md §6.1.1).

Templates reference a credential by name only. The value never enters a
template, a snapshot or an export, so exporting a flow cannot leak a token --
which is the whole reason the indirection exists.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import ApiCredential


class CredentialError(Exception):
    """A credential could not be stored or read."""


def _cipher() -> Fernet:
    """Derive the Fernet key from the configured secret.

    Deriving rather than requiring a pre-formatted key means operators set one
    environment variable, not two in a specific encoding.
    """
    secret = get_settings().session_secret
    if not secret or secret == "change-me":
        raise CredentialError(
            "SESSION_SECRET is unset or still the default; refusing to "
            "encrypt credentials with a known key"
        )
    digest = hashlib.sha256(secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(value: str) -> str:
    return _cipher().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _cipher().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise CredentialError(
            "credential could not be decrypted; SESSION_SECRET has probably "
            "changed since it was stored"
        ) from exc


async def put(
    session: AsyncSession,
    name: str,
    value: str,
    description: str = "",
    created_by_id: int | None = None,
) -> ApiCredential:
    row = (
        await session.scalars(
            select(ApiCredential).where(ApiCredential.name == name)
        )
    ).one_or_none()
    if row is None:
        row = ApiCredential(name=name, created_by_id=created_by_id)
        session.add(row)
    row.encrypted_value = encrypt(value)
    row.description = description or row.description
    await session.flush()
    return row


async def resolve(
    session: AsyncSession, names: set[str]
) -> dict[str, str]:
    """Decrypt the named credentials.

    A missing name is skipped rather than raised: the handler reports a clear
    configuration error, which is more useful than a stack trace from here.
    """
    if not names:
        return {}
    rows = (
        await session.scalars(
            select(ApiCredential).where(ApiCredential.name.in_(names))
        )
    ).all()
    return {row.name: decrypt(row.encrypted_value) for row in rows}


async def names(session: AsyncSession) -> list[str]:
    """Just the names, for the editor's credential picker."""
    rows = await session.scalars(
        select(ApiCredential.name).order_by(ApiCredential.name)
    )
    return list(rows.all())
