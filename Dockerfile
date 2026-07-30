# Backend image, shared by the api and worker services.
#
# One image, two entrypoints: they run the same code and must never drift out
# of step, so building them separately would only create the opportunity.

FROM python:3.12-slim AS base

# uv is copied from its own image rather than installed with pip, which keeps
# the layer small and the version pinned.
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Dependencies are installed before the source is copied so that editing code
# does not invalidate the dependency layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
COPY examples ./examples

RUN uv sync --frozen --no-dev

# Runs unprivileged: the shell_command handler exists, and even switched off it
# should never have had root available.
RUN useradd --create-home --uid 10001 gantt \
    && chown -R gantt:gantt /app
USER gantt

EXPOSE 8000

# Overridden per service in compose. The default is the API because that is
# what a bare `docker run` most likely means.
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
