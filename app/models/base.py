"""Declarative base and shared column types."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, Enum, MetaData, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON, TypeDecorator

# Explicit constraint naming so Alembic can autogenerate reversible
# migrations; without it, dropping an unnamed constraint is guesswork.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

#: JSONB on PostgreSQL, plain JSON elsewhere so the suite can run on SQLite.
JSONType = JSONB().with_variant(JSON(), "sqlite")


class UtcDateTime(TypeDecorator):
    """A timestamp that is always timezone aware in Python, always UTC in the
    database.

    PostgreSQL's ``TIMESTAMPTZ`` round-trips awareness; SQLite silently drops
    it and hands back a naive value. The scheduling engine refuses naive
    datetimes outright, so without normalising here the same code would work
    against one backend and raise against the other.

    Writing a naive value raises rather than guessing a zone: the caller knows
    what they meant, and this layer does not.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                f"refusing to store a naive datetime: {value!r}"
            )
        return value.astimezone(UTC)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


TZDateTime = UtcDateTime()


def enum_type(enum_cls: type[StrEnum]) -> Enum:
    """A VARCHAR-backed enum that round-trips as the Python member.

    Declaring these columns as plain ``String`` looks harmless because
    ``StrEnum`` compares equal to its value -- but a value loaded from the
    database comes back as a bare ``str``, and any ``is`` comparison against
    an enum member is then silently ``False``. That bug does not raise; it just
    quietly takes the wrong branch.

    No CHECK constraint: adding a status would otherwise need a schema
    migration, and the application already validates the values.
    """
    return Enum(
        enum_cls,
        native_enum=False,
        create_constraint=False,
        length=32,
        values_callable=lambda cls: [member.value for member in cls],
    )


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map = {
        dict[str, Any]: JSONType,
        list[Any]: JSONType,
        datetime: TZDateTime,
    }


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TZDateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
