from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

from skillwright_mcp.api import create_app
from skillwright_mcp.config import Settings
from skillwright_mcp.runtime import Runtime, build_runtime
from skillwright_mcp.workflow import WorkflowDefinition


class PersistOnlyDispatcher:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def submit(
        self,
        name: str,
        *,
        inputs: dict[str, Any] | None = None,
        version: int | None = None,
        idempotency_key: str | None = None,
        requested_by_principal_id: str | None = None,
    ) -> dict[str, Any]:
        return await self.runtime.engine.prepare_run(
            name,
            inputs=inputs,
            version=version,
            idempotency_key=idempotency_key,
            requested_by_principal_id=requested_by_principal_id,
        )

    async def cancel(self, run_id: str) -> dict[str, Any]:
        row = await self.runtime.database.request_cancel(run_id)
        if row is None:
            return {"status": "not_found", "run_id": run_id}
        return {
            "status": row.status,
            "run_id": row.id,
            "cancel_requested": row.cancel_requested,
        }


def _runtime_factory(settings: Settings) -> Runtime:
    runtime = build_runtime(settings)
    runtime.dispatcher = cast(Any, PersistOnlyDispatcher(runtime))
    return runtime


@pytest.mark.asyncio
async def test_control_api_create_status_cancel_uses_rbac_and_safe_run_shape(
    tmp_path: Path,
) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'api.db').as_posix()}",
        database_auto_create_schema=True,
        execution_backend="inline",
        allow_unauthenticated_local=True,
        local_principal="api-admin@example.test",
        local_role="admin",
    )
    app = create_app(settings, runtime_factory=_runtime_factory)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))

    async with app.router.lifespan_context(app):
        runtime = cast(Runtime, app.state.runtime)
        owner = await runtime.authorization.local_principal()
        await runtime.database.create_skill_version(
            WorkflowDefinition.model_validate(
                {
                    "name": "api-smoke",
                    "inputs": {"message": {"type": "string"}},
                    "steps": [{"op": "navigate", "url": "https://example.test/{{message}}"}],
                }
            ),
            owner_principal_id=owner.id,
        )

        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            live = await client.get("/health/live")
            assert live.status_code == 200
            assert live.json() == {"status": "live"}

            ready = await client.get("/health/ready")
            assert ready.status_code == 200
            assert ready.json() == {"status": "ready", "checks": {"database": "ok"}}

            created = await client.post(
                "/api/v1/runs",
                json={
                    "skill": "api-smoke",
                    "inputs": {"message": "private-value"},
                    "idempotency_key": "api-idempotent",
                },
            )
            assert created.status_code == 202
            body = created.json()
            assert body["status"] == "queued"
            assert "inputs" not in body
            assert "outputs" not in body
            assert "failure_context" not in body
            assert "private-value" not in created.text

            run_id = body["run_id"]
            fetched = await client.get(f"/api/v1/runs/{run_id}")
            assert fetched.status_code == 200
            assert fetched.json() == body

            cancelled = await client.post(f"/api/v1/runs/{run_id}/cancel")
            assert cancelled.status_code == 200
            assert cancelled.json()["status"] == "cancelled"
            assert cancelled.json()["cancel_requested"] is True


@pytest.mark.asyncio
async def test_liveness_stays_available_when_database_is_down() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://skillwright:skillwright@127.0.0.1:1/skillwright",
        execution_backend="inline",
        allow_unauthenticated_local=False,
        healthcheck_timeout_seconds=0.05,
    )
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client,
    ):
        live = await client.get("/health/live")
        assert live.status_code == 200
        ready = await client.get("/health/ready")
        assert ready.status_code == 503
        assert ready.json() == {
            "status": "not_ready",
            "checks": {"database": "unavailable"},
        }


@pytest.mark.asyncio
async def test_local_unauthenticated_control_access_is_loopback_only(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'external.db').as_posix()}",
        database_auto_create_schema=True,
        execution_backend="inline",
        allow_unauthenticated_local=True,
    )
    app = create_app(settings, runtime_factory=_runtime_factory)
    transport = httpx.ASGITransport(app=app, client=("203.0.113.10", 50000))

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://skillwright.test") as client,
    ):
        response = await client.post("/api/v1/runs", json={"skill": "missing"})
        assert response.status_code == 401
        assert response.json() == {"detail": {"code": "authentication_required"}}

        spoofed = await client.post(
            "/api/v1/runs",
            json={"skill": "missing"},
            headers={"x-principal": "local"},
        )
        assert spoofed.status_code == 401
        assert spoofed.json() == {"detail": {"code": "authentication_required"}}


@pytest.mark.asyncio
async def test_control_api_accepts_hashed_bearer_token_for_provisioned_principal(
    tmp_path: Path,
) -> None:
    token = "test-service-token-with-high-entropy-placeholder"
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'bearer-api.db').as_posix()}",
        database_auto_create_schema=True,
        execution_backend="inline",
        allow_unauthenticated_local=False,
        auth_token_hashes={token_hash: "api-service@example.test"},
    )
    app = create_app(settings, runtime_factory=_runtime_factory)
    transport = httpx.ASGITransport(app=app, client=("203.0.113.10", 50000))

    async with app.router.lifespan_context(app):
        runtime = cast(Runtime, app.state.runtime)
        principal = await runtime.database.ensure_principal("api-service@example.test", "admin")
        await runtime.database.create_skill_version(
            WorkflowDefinition.model_validate(
                {"name": "bearer-skill", "steps": [{"op": "navigate", "url": "https://example.test"}]}
            ),
            owner_principal_id=principal.id,
        )

        async with httpx.AsyncClient(
            transport=transport,
            base_url="https://skillwright.test",
        ) as client:
            unauthorized = await client.post(
                "/api/v1/runs",
                json={"skill": "bearer-skill"},
                headers={"authorization": "Bearer wrong-token"},
            )
            assert unauthorized.status_code == 401
            assert unauthorized.json() == {"detail": {"code": "invalid_token"}}

            created = await client.post(
                "/api/v1/runs",
                json={"skill": "bearer-skill"},
                headers={"authorization": f"Bearer {token}"},
            )
            assert created.status_code == 202
            assert created.json()["status"] == "queued"


def test_playwright_defaults_disable_automatic_action_snapshots() -> None:
    args = Settings().playwright_args()
    assert "--isolated" in args
    assert "--snapshot-mode=none" in args
