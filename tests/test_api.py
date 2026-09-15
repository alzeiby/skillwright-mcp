from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from sqlalchemy import select

from skillwright_mcp.api import create_app
from skillwright_mcp.config import Settings
from skillwright_mcp.db import AuditEventRow
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

    async def repair(
        self,
        run_id: str,
        *,
        step: int,
        replacement_element_id: str,
        persist: bool = True,
        actor_principal_id: str | None = None,
    ) -> dict[str, Any]:
        return await self.runtime.engine.request_repair(
            run_id,
            step=step,
            replacement_element_id=replacement_element_id,
            persist=persist,
            actor_principal_id=actor_principal_id,
        )

    async def decide_approval(
        self,
        approval_id: str,
        *,
        approve: bool,
        decided_by_principal_id: str,
        comment: str | None = None,
    ) -> dict[str, Any]:
        try:
            approval = await self.runtime.database.decide_approval(
                approval_id,
                approve=approve,
                decided_by_principal_id=decided_by_principal_id,
                comment=comment,
            )
        except (KeyError, ValueError):
            return {"status": "invalid_approval", "approval_id": approval_id}
        return {
            "status": "approved" if approve else "rejected",
            "approval_id": approval.id,
            "run_id": approval.run_id,
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


@pytest.mark.asyncio
async def test_control_api_repair_and_approval_interventions_are_authorized_and_safe(
    tmp_path: Path,
) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'interventions.db').as_posix()}",
        database_auto_create_schema=True,
        execution_backend="inline",
        allow_unauthenticated_local=True,
        local_principal="intervention-admin@example.test",
        local_role="admin",
    )
    app = create_app(settings, runtime_factory=_runtime_factory)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))

    async with app.router.lifespan_context(app):
        runtime = cast(Runtime, app.state.runtime)
        principal = await runtime.authorization.local_principal()
        original_requester = await runtime.database.ensure_principal(
            "original-requester@example.test",
            "developer",
        )
        skill, version = await runtime.database.create_skill_version(
            WorkflowDefinition.model_validate(
                {
                    "name": "intervention-skill",
                    "steps": [
                        {
                            "op": "click",
                            "target": {"role": "button", "name": "Original"},
                        }
                    ],
                }
            ),
            owner_principal_id=principal.id,
        )
        repair_run = await runtime.database.create_run(
            skill=skill,
            version=version,
            inputs={},
            status="queued",
            requested_by_principal_id=original_requester.id,
        )
        repair_context = {
            "status": "repair_required",
            "run_id": repair_run.id,
            "step": 0,
            "operation": "click",
            "expected": {"role": "button", "name": "Original"},
            "candidates": [
                {
                    "id": "candidate-0",
                    "name": "Replacement",
                    "target": {"role": "button", "name": "Replacement"},
                }
            ],
            "session_available": True,
        }
        await runtime.database.update_run(
            repair_run.id,
            status="repair_required",
            failure_context=repair_context,
        )

        approval_run = await runtime.database.create_run(
            skill=skill,
            version=version,
            inputs={},
            status="queued",
            requested_by_principal_id=principal.id,
        )
        approval = await runtime.database.get_or_create_approval(
            run_id=approval_run.id,
            workflow_version_id=version.id,
            step_index=0,
            gate_fingerprint="a" * 64,
            reason="External side effect",
            requested_by_principal_id=principal.id,
        )
        await runtime.database.update_run(
            approval_run.id,
            status="approval_required",
            failure_context={
                "status": "approval_required",
                "run_id": approval_run.id,
                "step": 0,
                "operation": "click",
                "approval_id": approval.id,
                "reason": approval.reason,
                "target": {"role": "button", "name": "Original"},
                "session_available": True,
            },
        )

        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            repair_view = await client.get(
                f"/api/v1/runs/{repair_run.id}/intervention"
            )
            assert repair_view.status_code == 200
            assert repair_view.json()["type"] == "repair"
            assert repair_view.json()["candidates"][0]["id"] == "candidate-0"

            repair = await client.post(
                f"/api/v1/runs/{repair_run.id}/repair",
                json={
                    "step": 0,
                    "replacement_element_id": "candidate-0",
                    "persist": True,
                },
            )
            assert repair.status_code == 200
            assert repair.json()["status"] == "repair_pending"
            assert "candidates" not in repair.text

            async with runtime.database.sessions() as session:
                repair_audit = await session.scalar(
                    select(AuditEventRow).where(
                        AuditEventRow.event_type == "repair.requested",
                        AuditEventRow.entity_id == repair.json()["repair_id"],
                    )
                )
            assert repair_audit is not None
            assert repair_audit.principal_id == principal.id
            assert repair_audit.principal_id != original_requester.id

            approval_view = await client.get(
                f"/api/v1/runs/{approval_run.id}/intervention"
            )
            assert approval_view.status_code == 200
            assert approval_view.json()["type"] == "approval"
            assert approval_view.json()["approval_id"] == approval.id

            rejected = await client.post(
                f"/api/v1/approvals/{approval.id}/decision",
                json={"approve": False, "comment": "Reviewed and rejected"},
            )
            assert rejected.status_code == 200
            assert rejected.json() == {
                "status": "rejected",
                "approval_id": approval.id,
                "run_id": approval_run.id,
            }

            stored_approval = await runtime.database.get_approval(approval.id)
            assert stored_approval is not None
            assert stored_approval.status == "rejected"
            assert stored_approval.comment == "Reviewed and rejected"


def test_playwright_defaults_disable_automatic_action_snapshots() -> None:
    args = Settings().playwright_args()
    assert "--isolated" in args
    assert "--snapshot-mode=none" in args
