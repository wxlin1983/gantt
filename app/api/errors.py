"""One error shape for the whole API (implement.md §8).

Every failure comes back as ``{"error": {"code", "message", "details"}}`` with
a domain code, so the frontend can branch on the code and show the location
rather than parsing prose.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.dsl.errors import DslError
from app.services.cases import CaseError
from app.services.snapshot import SnapshotError

#: Domain codes that are the caller's fault in a specific way. Anything not
#: listed is treated as a 400.
_STATUS_BY_CODE = {
    "E_CASE_NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "E_TASK_NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "E_TEMPLATE_NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "E_ALREADY_DONE": status.HTTP_409_CONFLICT,
    "E_NOT_ACTIVE": status.HTTP_409_CONFLICT,
    "E_STALE_WRITE": status.HTTP_409_CONFLICT,
    "E_FORBIDDEN": status.HTTP_403_FORBIDDEN,
    "E_UNAUTHENTICATED": status.HTTP_401_UNAUTHORIZED,
}


class ApiError(Exception):
    """Raised by routers for failures that are not service-level."""

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
        http_status: int | None = None,
    ):
        self.code = code
        self.message = message
        self.details = details or {}
        self.http_status = http_status or _STATUS_BY_CODE.get(
            code, status.HTTP_400_BAD_REQUEST
        )
        super().__init__(message)


def payload(
    code: str, message: str, details: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
        }
    }


def install(app: FastAPI) -> None:
    """Register the handlers that keep the error shape consistent."""

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content=payload(exc.code, exc.message, exc.details),
        )

    @app.exception_handler(CaseError)
    async def _case_error(_: Request, exc: CaseError) -> JSONResponse:
        return JSONResponse(
            status_code=_STATUS_BY_CODE.get(
                exc.code, status.HTTP_400_BAD_REQUEST
            ),
            content=payload(exc.code, str(exc)),
        )

    @app.exception_handler(DslError)
    async def _dsl_error(_: Request, exc: DslError) -> JSONResponse:
        first = exc.issues[0] if exc.issues else None
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=payload(
                first.code if first else "E_INVALID_TEMPLATE",
                first.message if first else str(exc),
                {
                    "issues": [
                        {
                            "code": issue.code,
                            "message": issue.message,
                            "path": issue.path,
                            "severity": issue.severity,
                        }
                        for issue in exc.issues
                    ]
                },
            ),
        )

    @app.exception_handler(SnapshotError)
    async def _snapshot_error(_: Request, exc: SnapshotError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=payload("E_BAD_SNAPSHOT", str(exc)),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Reshaped into the domain envelope so clients only parse one format.
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=payload(
                "E_BAD_REQUEST",
                "request body failed validation",
                {
                    "issues": [
                        {
                            "path": ".".join(
                                str(part) for part in error["loc"]
                            ),
                            "message": error["msg"],
                        }
                        for error in exc.errors()
                    ]
                },
            ),
        )
