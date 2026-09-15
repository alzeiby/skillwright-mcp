from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import select

import skillwright_mcp.queue as queue_module
from skillwright_mcp.browser import BrowserController
from skillwright_mcp.config import Settings
from skillwright_mcp.db import AuditEventRow, Database, RunRow
from skillwright_mcp.engine import WorkflowEngine
from skillwright_mcp.queue import RunDispatcher, broker, execute_run_task
from skillwright_mcp.workflow import WorkflowDefinition


class NeverPlaywright:
    async def call(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("queue publication must not execute browser work")

    async def has_tool(self, _name: str) -> bool:
        return False


class ClosingPlaywright:
    def __init__(self) -> None:
        self.close_calls = 0

    async def call(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("worker exception test must fail before browser work")

    async def has_tool(self, _name: str) -> bool:
        return False

    async def close(self) -> None:
        self.close_calls += 1


async def _dispatcher(tmp_path: Path) -> tuple[Database, RunDispatcher]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'queue.db').as_posix()}")
    await database.initialize(create_schema=True)
    workflow = WorkflowDefinition.model_validate(
        {
            "name": "queue-smoke",
            "steps": [{"op": "navigate", "url": "https://example.test"}],
        }
    )
    await database.create_skill_version(workflow)
    browser = BrowserController(cast(Any, NeverPlaywright()), database)
    engine = WorkflowEngine(database, browser)
    dispatcher = RunDispatcher(
        settings=Settings(
            database_url=database.url,
            execution_backend="redis",
        ),
        database=database,
        engine=engine,
    )
    return database, dispatcher


@pytest.mark.asyncio
async def test_publish_failure_is_recoverable_and_does_not_persist_broker_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, dispatcher = await _dispatcher(tmp_path)
    sentinel = "redis://user:do-not-persist-me@example.test/0"

    async def fail_publish(_dispatcher: RunDispatcher, _run_id: str) -> str:
        raise ConnectionError(sentinel)

    async def succeed_publish(_dispatcher: RunDispatcher, run_id: str) -> str:
        return f"task-{run_id}"

    try:
        monkeypatch.setattr(RunDispatcher, "_send_task", fail_publish)
        failed = await dispatcher.submit("queue-smoke")
        assert failed["status"] == "queue_unavailable"
        assert failed["queue_error_type"] == "ConnectionError"
        assert sentinel not in str(failed)

        run_id = str(failed["run_id"])
        stored = await database.get_run(run_id)
        assert stored is not None
        assert stored.status == "retrying"
        assert stored.failure_context == {
            "status": "queue_unavailable",
            "reason": "queue_publish_failed",
            "error_type": "ConnectionError",
            "side_effect_state": "not_started",
        }
        assert sentinel not in str(stored.failure_context)

        monkeypatch.setattr(RunDispatcher, "_send_task", succeed_publish)
        assert await dispatcher.recover_publish_failures() == [run_id]
        recovered = await database.get_run(run_id)
        assert recovered is not None
        assert recovered.status == "queued"
        assert recovered.failure_context is None
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_idempotent_retry_republishes_retrying_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, dispatcher = await _dispatcher(tmp_path)
    attempts = 0

    async def flaky_publish(_dispatcher: RunDispatcher, run_id: str) -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("down")
        return f"task-{run_id}"

    try:
        monkeypatch.setattr(RunDispatcher, "_send_task", flaky_publish)
        first = await dispatcher.submit("queue-smoke", idempotency_key="same-request")
        assert first["status"] == "queue_unavailable"

        second = await dispatcher.submit("queue-smoke", idempotency_key="same-request")
        assert second["status"] == "queued"
        assert second["run_id"] == first["run_id"]
        assert attempts == 2

        stored = await database.get_run(str(first["run_id"]))
        assert stored is not None
        assert stored.status == "queued"
        assert stored.failure_context is None
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_dispatcher_publishes_with_broker_built_from_its_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'custom-broker.db').as_posix()}")
    await database.initialize(create_schema=True)
    browser = BrowserController(cast(Any, NeverPlaywright()), database)
    engine = WorkflowEngine(database, browser)
    settings = Settings(
        database_url=database.url,
        execution_backend="redis",
        redis_url="redis://custom-redis.invalid:6388/7",
        redis_queue_name="custom-skillwright-runs",
    )
    dispatcher = RunDispatcher(settings=settings, database=database, engine=engine)

    try:
        publisher_broker = dispatcher.publisher_broker
        publish_task = dispatcher._publish_task
        assert publisher_broker is not None
        assert publish_task is not None
        assert publisher_broker is not broker
        assert publish_task.broker is publisher_broker
        assert execute_run_task.broker is broker
        assert publisher_broker.queue_name == "custom-skillwright-runs"
        assert publisher_broker.connection_pool.connection_kwargs["host"] == "custom-redis.invalid"
        assert publisher_broker.connection_pool.connection_kwargs["port"] == 6388
        assert publisher_broker.connection_pool.connection_kwargs["db"] == 7

        published: list[Any] = []

        async def capture(message: Any) -> None:
            published.append(message)

        monkeypatch.setattr(publisher_broker, "kick", capture)
        task_id = await dispatcher._send_task("custom-run")

        assert task_id == "skillwright-run-custom-run"
        assert published
        assert published[0].task_name == "skillwright.execute_run"
        assert b"custom-run" in published[0].message
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_worker_exception_persists_only_error_type_and_closes_browser_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "worker-error.db"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    database = Database(database_url)
    await database.initialize(create_schema=True)
    workflow = WorkflowDefinition.model_validate(
        {
            "name": "worker-error",
            "steps": [{"op": "navigate", "url": "https://example.test"}],
        }
    )
    skill, version = await database.create_skill_version(workflow)
    run = await database.create_run(
        skill=skill,
        version=version,
        inputs={},
        status="queued",
    )
    run_id = run.id
    await database.close()

    settings = Settings(
        database_url=database_url,
        database_auto_create_schema=False,
        execution_backend="redis",
    )
    fake = ClosingPlaywright()
    client_settings: list[Settings] = []
    sentinel = "worker-secret-do-not-persist"

    async def fail_execution(
        engine: WorkflowEngine,
        run_id: str,
        *,
        worker_id: str,
    ) -> dict[str, Any]:
        claimed = await engine.database.claim_run(run_id, worker_id)
        assert claimed is not None
        raise RuntimeError(sentinel)

    monkeypatch.setattr(queue_module, "Settings", lambda: settings)

    def build_fake_client(resolved_settings: Settings) -> Any:
        client_settings.append(resolved_settings)
        return fake

    monkeypatch.setattr(queue_module, "PlaywrightMCPClient", build_fake_client)
    monkeypatch.setattr(WorkflowEngine, "execute_persisted_run", fail_execution)

    result = await queue_module.execute_run(run_id)

    assert result["status"] == "failed_unknown"
    assert result["error_type"] == "RuntimeError"
    assert sentinel not in str(result)
    assert fake.close_calls == 1
    assert len(client_settings) == 1
    expected_directory = (
        settings.playwright_output_dir
        / "runs"
        / hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    )
    assert client_settings[0].playwright_output_dir == expected_directory
    assert run_id not in str(expected_directory)

    persisted = Database(database_url)
    try:
        stored = await persisted.get_run(run_id)
        assert stored is not None
        assert stored.status == "failed_unknown"
        assert stored.failure_context is not None
        assert stored.failure_context["error_type"] == "RuntimeError"
        assert sentinel not in str(stored.failure_context)
    finally:
        await persisted.close()

    assert sentinel.encode() not in database_path.read_bytes()


@pytest.mark.asyncio
async def test_duplicate_delivery_cannot_emit_owner_teardown_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "duplicate-worker-marker.db"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    database = Database(database_url)
    await database.initialize(create_schema=True)
    workflow = WorkflowDefinition.model_validate(
        {
            "name": "duplicate-worker-marker",
            "steps": [{"op": "navigate", "url": "about:blank"}],
        }
    )
    skill, version = await database.create_skill_version(workflow)
    run = await database.create_run(skill=skill, version=version, inputs={}, status="queued")
    async with database.sessions.begin() as session:
        row = await session.get(RunRow, run.id)
        assert row is not None
        row.status = "succeeded"
        row.worker_id = "true-owner"
    await database.close()

    settings = Settings(
        database_url=database_url,
        database_auto_create_schema=False,
        execution_backend="redis",
    )
    fake = ClosingPlaywright()

    async def ignored_delivery(
        _engine: WorkflowEngine,
        run_id: str,
        *,
        worker_id: str,
    ) -> dict[str, Any]:
        return {"status": "succeeded", "run_id": run_id, "worker_id": worker_id}

    monkeypatch.setattr(queue_module, "Settings", lambda: settings)
    monkeypatch.setattr(queue_module, "PlaywrightMCPClient", lambda _settings: fake)
    monkeypatch.setattr(WorkflowEngine, "execute_persisted_run", ignored_delivery)

    result = await queue_module.execute_run(run.id)
    assert result["status"] == "succeeded"
    assert fake.close_calls == 1

    persisted = Database(database_url)
    try:
        async with persisted.sessions() as session:
            markers = (
                await session.scalars(
                    select(AuditEventRow).where(
                        AuditEventRow.event_type == "skill.run.worker_finished",
                        AuditEventRow.entity_id == run.id,
                    )
                )
            ).all()
        assert markers == []
    finally:
        await persisted.close()


@pytest.mark.asyncio
async def test_worker_exception_cannot_overwrite_replacement_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "worker-exception-owner-race.db"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    database = Database(database_url)
    await database.initialize(create_schema=True)
    workflow = WorkflowDefinition.model_validate(
        {
            "name": "worker-exception-owner-race",
            "steps": [{"op": "navigate", "url": "about:blank"}],
        }
    )
    skill, version = await database.create_skill_version(workflow)
    run = await database.create_run(skill=skill, version=version, inputs={}, status="queued")
    await database.close()

    settings = Settings(
        database_url=database_url,
        database_auto_create_schema=False,
        execution_backend="redis",
    )
    fake = ClosingPlaywright()

    async def fail_after_claim(
        engine: WorkflowEngine,
        run_id: str,
        *,
        worker_id: str,
    ) -> dict[str, Any]:
        claimed = await engine.database.claim_run(run_id, worker_id)
        assert claimed is not None
        raise RuntimeError("stale worker crash")

    original_update_owned_run = Database.update_owned_run
    raced = False

    async def replace_owner_before_failure_write(
        self: Database,
        run_id: str,
        worker_id: str,
        **kwargs: Any,
    ) -> bool:
        nonlocal raced
        if kwargs.get("status") == "failed_unknown" and not raced:
            raced = True
            async with self.sessions.begin() as session:
                row = await session.get(RunRow, run_id)
                assert row is not None
                row.status = "running"
                row.worker_id = "replacement-owner"
            return await original_update_owned_run(self, run_id, worker_id, **kwargs)
        return await original_update_owned_run(self, run_id, worker_id, **kwargs)

    monkeypatch.setattr(queue_module, "Settings", lambda: settings)
    monkeypatch.setattr(queue_module, "PlaywrightMCPClient", lambda _settings: fake)
    monkeypatch.setattr(WorkflowEngine, "execute_persisted_run", fail_after_claim)
    monkeypatch.setattr(Database, "update_owned_run", replace_owner_before_failure_write)

    result = await queue_module.execute_run(run.id)
    assert result == {
        "status": "ignored",
        "run_id": run.id,
        "reason": "run_not_owned_by_worker",
    }
    assert raced is True
    assert fake.close_calls == 1

    persisted = Database(database_url)
    try:
        stored = await persisted.get_run(run.id)
        assert stored is not None
        assert stored.status == "running"
        assert stored.worker_id == "replacement-owner"
    finally:
        await persisted.close()
