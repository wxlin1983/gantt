"""External result callbacks (implement.md §6.5).

Deliberately unauthenticated: the caller is a third-party system, not a user.
The token is the credential -- single use, bound to one run, and expiring with
that run's timeout. It is therefore never a general-purpose API key, and a
replayed POST finds the token already burned.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Path
from pydantic import Field

from app.execution import runner

from ..deps import SessionDep
from ..errors import ApiError
from ..schemas import Body

router = APIRouter(prefix="/callbacks", tags=["callbacks"])


class CallbackRequest(Body):
    status: Literal["succeeded", "failed"]
    payload: dict[str, Any] = Field(default_factory=dict)
    message: str = ""


@router.post("/{token}")
async def resolve(
    session: SessionDep,
    body: CallbackRequest,
    token: Annotated[str, Path(min_length=16, max_length=128)],
) -> dict[str, str]:
    try:
        outcome = await runner.resolve_callback(
            session,
            token,
            outcome=body.status,
            payload=body.payload,
            message=body.message,
        )
    except LookupError as exc:
        # The same answer whether the token never existed or was already used:
        # a caller does not need to be able to probe for live tokens.
        raise ApiError(
            "E_BAD_CALLBACK_TOKEN",
            "this callback token is unknown, already used, or expired",
        ) from exc
    return {"status": outcome.status, "detail": outcome.detail}
