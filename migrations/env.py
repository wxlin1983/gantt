"""Alembic environment.

The database URL comes from application settings rather than alembic.ini, so
there is a single source of truth for it.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config import get_settings
from app.models import Base
from app.models.base import UtcDateTime

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Escape % so ConfigParser does not treat it as interpolation syntax.
config.set_main_option(
    "sqlalchemy.url", get_settings().database_url.replace("%", "%%")
)

target_metadata = Base.metadata


def render_item(type_, obj, autogen_context) -> str | bool:
    """Render our shared column types by name.

    Left to itself, Alembic emits the fully qualified constructor --
    ``postgresql.JSONB(astext_type=Text())`` or
    ``app.models.base.UtcDateTime(...)`` -- without importing what those names
    need, producing a migration that fails at import time. Naming the shared
    types also keeps their dialect variants in one place.
    """
    if type_ != "type":
        return False
    if isinstance(obj, UtcDateTime):
        autogen_context.imports.add(
            "from app.models.base import TZDateTime"
        )
        return "TZDateTime"
    if isinstance(obj, JSONB):
        autogen_context.imports.add("from app.models.base import JSONType")
        return "JSONType"
    return False


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # Without these, a changed column type or server default is silently
        # missed by autogenerate.
        compare_type=True,
        compare_server_default=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
