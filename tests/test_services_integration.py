from __future__ import annotations

import asyncio
import os
from typing import Any, cast
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from redis.asyncio import Redis
from sqlalchemy import func, select, text

from skillwright_mcp.auth import AuthorizationService
from skillwright_mcp.config import Settings
from skillwright_mcp.db import SCHEMA_REVISION, AuditEventRow, BrowserActionRow, Database
from skillwright_mcp.engine import WorkflowEngine
from skillwright_mcp.queue import RunDispatcher
from skillwright_mcp.runtime import build_runtime
from skillwright_mcp.workflow import WorkflowDefinition


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
    assert expected_head == SCHEMA_REVISION

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


@pytest.mark.asyncio
async def test_runtime_readiness_verifies_database_schema_head() -> None:
    _require_service_integration()
    runtime = build_runtime(Settings())
    try:
        checks = await runtime.readiness()
        assert checks == {
            "database": "ok",
            "schema": "ok",
            "redis": "ok",
        }
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_taskiq_worker_executes_concurrent_published_runs_to_postgres() -> None:
    _require_service_integration()
    settings = Settings()
    assert settings.execution_backend == "redis"

    database = Database(settings.database_url)
    await database.initialize(create_schema=False)
    authorization = AuthorizationService(database, settings)
    dispatcher: RunDispatcher | None = None
    try:
        principal = await database.ensure_principal(
            f"ci:worker:{uuid4().hex}",
            role="developer",
        )
        engine = WorkflowEngine(database, cast(Any, None), authorization)
        dispatcher = RunDispatcher(
            settings=settings,
            database=database,
            engine=engine,
        )
        await dispatcher.start()

        run_ids: list[str] = []
        for index in range(2):
            workflow = WorkflowDefinition.model_validate(
                {
                    "name": f"ci-worker-{index}-{uuid4().hex}",
                    "steps": [
                        {"op": "navigate", "url": "about:blank"},
                        {"op": "wait", "seconds": 0.75},
                    ],
                }
            )
            await database.create_skill_version(
                workflow,
                owner_principal_id=principal.id,
                actor_principal_id=principal.id,
            )
            submitted = await dispatcher.submit(
                workflow.name,
                requested_by_principal_id=principal.id,
                idempotency_key=f"ci-worker-request-{uuid4().hex}",
            )
            assert submitted["status"] == "queued"
            run_ids.append(str(submitted["run_id"]))

        deadline = asyncio.get_running_loop().time() + 30
        runs = [await database.get_run(run_id) for run_id in run_ids]
        worker_finished: dict[str, bool] = {run_id: False for run_id in run_ids}
        while True:
            runs = [await database.get_run(run_id) for run_id in run_ids]
            async with database.sessions() as session:
                markers = (
                    await session.scalars(
                        select(AuditEventRow).where(
                            AuditEventRow.event_type == "skill.run.worker_finished",
                            AuditEventRow.entity_type == "run",
                            AuditEventRow.entity_id.in_(run_ids),
                        )
                    )
                ).all()
            for run_id, run in zip(run_ids, runs, strict=True):
                worker_finished[run_id] = bool(
                    run is not None
                    and run.worker_id is not None
                    and any(
                        marker.entity_id == run_id
                        and (marker.data or {}).get("worker_id") == run.worker_id
                        for marker in markers
                    )
                )
            if all(
                run is not None
                and run.status in {"succeeded", "failed", "failed_unknown", "cancelled"}
                and worker_finished[run_id]
                for run_id, run in zip(run_ids, runs, strict=True)
            ):
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError(
                    "workers did not finish runs and browser teardown; "
                    f"last statuses={[run.status if run is not None else None for run in runs]!r}, "
                    f"worker_finished={worker_finished!r}"
                )
            await asyncio.sleep(0.1)

        assert all(run is not None for run in runs)
        completed_runs = [run for run in runs if run is not None]
        assert all(worker_finished.values())
        assert all(run.status == "succeeded" for run in completed_runs), [
            run.failure_context for run in completed_runs
        ]
        assert all(run.started_at is not None for run in completed_runs)
        assert all(run.finished_at is not None for run in completed_runs)
        assert all(run.attempt_count == 1 for run in completed_runs)
        started_at = [run.started_at for run in completed_runs if run.started_at is not None]
        finished_at = [run.finished_at for run in completed_runs if run.finished_at is not None]
        assert max(started_at) < min(finished_at), "production concurrency=2 was not exercised"
        async with database.sessions() as session:
            for run_id in run_ids:
                action_count = await session.scalar(
                    select(func.count()).select_from(BrowserActionRow).where(
                        BrowserActionRow.run_id == run_id,
                        BrowserActionRow.source == "replay",
                    )
                )
                assert action_count == 2
    finally:
        if dispatcher is not None:
            await dispatcher.close()
        await database.close()
