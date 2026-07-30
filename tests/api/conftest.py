"""HTTP-level fixtures."""

from __future__ import annotations

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_session
from app.api.main import create_app


@pytest_asyncio.fixture
async def app(session):
    """The real application, wired to the test session.

    The dependency override hands out the same session the fixtures seeded, so
    a request sees the seeded rows without committing anything.
    """
    application = create_app()

    async def _session_override():
        yield session

    application.dependency_overrides[get_session] = _session_override
    return application


@pytest_asyncio.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test/api/v1"
    ) as client:
        yield client


@pytest_asyncio.fixture
async def as_admin(client, seeded):
    await _login(client, "admin", "admin-pw")
    return client


@pytest_asyncio.fixture
async def as_pm(client, seeded):
    await _login(client, "pm", "pm-pw")
    return client


@pytest_asyncio.fixture
async def as_qa(client, seeded):
    await _login(client, "qa", "qa-pw")
    return client


@pytest_asyncio.fixture
async def as_outsider(client, seeded):
    await _login(client, "outsider", "out-pw")
    return client


async def _login(client: AsyncClient, username: str, password: str) -> None:
    response = await client.post(
        "/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
