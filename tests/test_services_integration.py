from __future__ import annotations

import os

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from redis.asyncio import Redis
from sqlalchemy import text

from skillwright_mcp.config import Settings
from skillwright_mcp.db import Database


def _require_service_integration() -> None:
    if os.environ.get("SKILLWRIGHT_INTEGRATION_SERVICES") != "1":
        pytest.skip("set SKILLWRIGHT_INTEGRATION_SERVICES=1 to run service integration tests")


@pytest.mark.asyncio
async def test_postgres_is_migrated_to_alembic_head_and_usable() -> None:
    _require_service_integration()
    settings = Settings()
    assert settings.database_url.startswith("postgresql+asyncpg://")

    script = ScriptDirectory.from_config(Config("alembic.ini"))
    expected_head = script.get_current_head()
    assert expected_head is not None

    database = Database(settings.database_url)
    try:
        await database.initialize(create_schema=False)
        async with database.sessions() as session:
            current_head = await session.scalar(text("SELECT version_num FROM alembic_version"))
        assert current_head == expected_head

        principal = await database.ensure_principal("ci:service-integration", role="viewer")
        loaded = await database.get_principal_by_external_key("ci:service-integration")
        assert loaded is not None
        assert loaded.id == principal.id
        assert loaded.role == "viewer"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_redis_round_trip() -> None:
    _require_service_integration()
    settings = Settings()
    client = Redis.from_url(settings.redis_url, decode_responses=True)
    key = "skillwright:ci:service-integration"
    try:
        assert await client.ping() is True
        await client.set(key, "ok", ex=30)
        assert await client.get(key) == "ok"
    finally:
        await client.delete(key)
        await client.aclose()
