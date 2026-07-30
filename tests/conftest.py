"""Database fixtures.

The suite runs on SQLite: the models declare dialect variants for their JSON
columns and partial indexes, so the same metadata builds on both engines. Tests
that depend on PostgreSQL-only behaviour (``SKIP LOCKED``) belong in a separate
integration suite rather than here.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.auth.passwords import hash_password
from app.config import get_settings
from app.models import (
    Base,
    GanttTemplateRecord,
    Group,
    GroupMember,
    TaskTemplateRecord,
    TemplateStatus,
    User,
)
from app.services import calendars as calendar_service


@pytest.fixture(autouse=True)
def session_secret(monkeypatch):
    """Give the suite a real secret.

    Credential encryption refuses to run with the default value, which is the
    behaviour we want in production and therefore also what the tests must
    satisfy rather than bypass.
    """
    monkeypatch.setenv("SESSION_SECRET", "test-secret-not-for-production")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session(engine) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session


@pytest_asyncio.fixture
async def seeded(session: AsyncSession) -> dict:
    """A minimal but realistic installation.

    Two groups, four users with distinct roles, the builtin calendars, three
    task templates and one published gantt template.
    """
    await calendar_service.ensure_builtins(session)

    admin = User(
        username="admin",
        display_name="Admin",
        email="admin@example.com",
        password_hash=hash_password("admin-pw"),
        is_template_admin=True,
    )
    pm = User(
        username="pm",
        display_name="Project Manager",
        email="pm@example.com",
        password_hash=hash_password("pm-pw"),
    )
    qa = User(
        username="qa",
        display_name="QA Engineer",
        email="qa@example.com",
        password_hash=hash_password("qa-pw"),
    )
    outsider = User(
        username="outsider",
        display_name="Outsider",
        email="out@example.com",
        password_hash=hash_password("out-pw"),
    )
    session.add_all([admin, pm, qa, outsider])
    await session.flush()

    rnd = Group(name="rnd", display_name="R&D")
    quality = Group(name="quality", display_name="Quality")
    session.add_all([rnd, quality])
    await session.flush()

    session.add_all(
        [
            GroupMember(group_id=rnd.id, user_id=pm.id, is_lead=True),
            GroupMember(group_id=quality.id, user_id=qa.id, is_lead=True),
        ]
    )

    for name, duration, api in (
        ("tt_plan", "12H", None),
        ("tt_test", "16H", "http_request"),
        ("tt_report", "12H", None),
    ):
        session.add(
            TaskTemplateRecord(
                name=name,
                display_name=name.replace("tt_", "").title(),
                duration_default=duration,
                task_api=api,
                api_mode="trigger_poll" if api else None,
            )
        )
    await session.flush()

    template = GanttTemplateRecord(
        name="launch",
        version=1,
        status=TemplateStatus.PUBLISHED,
        definition=LAUNCH_TEMPLATE,
        created_by_id=admin.id,
        published_at=datetime.now(tz=UTC),
    )
    session.add(template)
    await session.flush()

    return {
        "admin": admin,
        "pm": pm,
        "qa": qa,
        "outsider": outsider,
        "rnd": rnd,
        "quality": quality,
        "template": template,
    }


#: Exercises roles, phases, a conditional task, lag and a project buffer.
LAUNCH_TEMPLATE = {
    "template_name": "launch",
    "version": 1,
    "buffer": "8H",
    "roles": [
        {"name": "owner", "display_name": "Owner", "required": True},
        {"name": "tester", "display_name": "Tester", "required": False},
    ],
    "template_para": [
        {
            "para_name": "test_hours",
            "para_type": "int",
            "para_default": 16,
            "validation": {"min": 1, "max": 100},
        },
        {
            "para_name": "needs_review",
            "para_type": "bool",
            "para_default": True,
        },
    ],
    "flow": [
        {
            "phase": "Prepare",
            "tasks": [
                {
                    "id": "plan",
                    "uses": "tt_plan",
                    "label": "Planning",
                    "owner": {"role": "owner"},
                    "group": "rnd",
                    "duration": "12H",
                    "requirement": "none",
                }
            ],
        },
        {
            "phase": "Verify",
            "tasks": [
                {
                    "id": "test",
                    "uses": "tt_test",
                    "label": "Testing",
                    "owner": {"role": "tester"},
                    "group": "quality",
                    "duration": "{{ para.test_hours }}H",
                    "requirement": [{"task": "plan", "lag": "4H"}],
                },
                {
                    "id": "review",
                    "uses": "tt_plan",
                    "label": "Review",
                    "owner": {"group_lead": "quality"},
                    "group": "quality",
                    "duration": "6H",
                    "when": "{{ para.needs_review }}",
                    "requirement": "plan",
                },
            ],
        },
        {
            "phase": "Close",
            "tasks": [
                {
                    "id": "report",
                    "uses": "tt_report",
                    "label": "Report",
                    "owner": {"same_as": "plan"},
                    "group": "rnd",
                    "duration": "12H",
                    "requirement": ["test", "review"],
                },
                {
                    "id": "notify",
                    "uses": "tt_report",
                    "label": "Notify",
                    "owner": {"role": "owner"},
                    "duration": "10M",
                    "requirement": "report",
                    "optional": True,
                    "on_failure": "continue",
                },
            ],
        },
    ],
}
