"""FastAPI application factory."""

from __future__ import annotations

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.execution.registry import load_builtin_handlers

from . import errors
from .routers import auth, callbacks, cases, directory, templates

API_PREFIX = "/api/v1"


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Gantt",
        version="0.1.0",
        description="Template-driven gantt workflow system",
        docs_url=f"{API_PREFIX}/docs",
        openapi_url=f"{API_PREFIX}/openapi.json",
    )

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="gantt_session",
        same_site="lax",
        # Set in production; left off locally so http://localhost works.
        https_only=False,
        max_age=60 * 60 * 12,
    )

    errors.install(app)
    # Registering the built-in handlers here means /handlers is populated for
    # the API process too, not only for the worker.
    load_builtin_handlers()

    app.include_router(auth.router, prefix=API_PREFIX)
    app.include_router(cases.router, prefix=API_PREFIX)
    app.include_router(directory.router, prefix=API_PREFIX)
    app.include_router(templates.router, prefix=API_PREFIX)
    app.include_router(callbacks.router, prefix=API_PREFIX)

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, str]:
        """Liveness only: no database round trip, so it stays useful when the
        database is the thing that is down."""
        return {"status": "ok"}

    return app


app = create_app()
