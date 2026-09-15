from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, update

from skillwright_mcp.db import BrowserActionRow, Database, RunRow
from skillwright_mcp.workflow import WorkflowDefinition


async def _database_with_skill(tmp_path: Path) -> tuple[Database, object, object]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'distributed.db').as_posix()}")
    await database.initialize(create_schema=True)
    workflow = WorkflowDefinition.model_validate(
        {
            "name": "distributed-smoke",
            "steps": [{"op": "navigate", "url": "https://example.com"}],
        }
    )
    skill, version = await database.create_skill_version(workflow)
    return database, skill, version


@pytest.mark.asyncio
async def test_run_idempotency_claim_and_queued_cancel(tmp_path: Path) -> None:
    database, skill, version = await _database_with_skill(tmp_path)
    try:
        first = await database.create_run(
            skill=skill,
            version=version,
            inputs={},
            status="queued",
            idempotency_key="same-request",
        )
        duplicate = await database.create_run(
            skill=skill,
            version=version,
            inputs={},
            status="queued",
            idempotency_key="same-request",
        )
        assert duplicate.id == first.id
        assert first.started_at is None

        claimed = await database.claim_run(first.id, "worker-a")
        assert claimed is not None
        assert claimed.worker_id == "worker-a"
        assert claimed.attempt_count == 1
        assert claimed.started_at is not None
        assert await database.claim_run(first.id, "worker-b") is None

        queued = await database.create_run(
            skill=skill,
            version=version,
            inputs={},
            status="queued",
            idempotency_key="cancel-request",
        )
        cancelled = await database.request_cancel(queued.id)
        assert cancelled is not None
        assert cancelled.status == "cancelled"
        assert cancelled.cancel_requested is True
        assert cancelled.finished_at is not None
        assert await database.claim_run(queued.id, "worker-a") is None
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_stale_run_requeues_only_before_mutating_browser_work(tmp_path: Path) -> None:
    database, skill, version = await _database_with_skill(tmp_path)
    old_heartbeat = datetime.now(UTC) - timedelta(minutes=30)
    try:
        safe = await database.create_run(
            skill=skill,
            version=version,
            inputs={},
            status="queued",
            idempotency_key="safe-stale",
        )
        assert await database.claim_run(safe.id, "dead-worker") is not None
        async with database.sessions.begin() as session:
            await session.execute(
                update(RunRow).where(RunRow.id == safe.id).values(heartbeat_at=old_heartbeat)
            )

        recovered = await database.recover_stale_runs(60)
        assert recovered == {
            "requeued": [safe.id],
            "failed_unknown": [],
            "repair_session_expired": [],
            "approval_session_expired": [],
        }
        safe_after = await database.get_run(safe.id)
        assert safe_after is not None
        assert safe_after.status == "queued"
        assert safe_after.worker_id is None
        first_started_at = safe_after.started_at
        assert first_started_at is not None

        reclaimed = await database.claim_run(safe.id, "replacement-worker")
        assert reclaimed is not None
        assert reclaimed.started_at == first_started_at
        async with database.sessions.begin() as session:
            await session.execute(
                update(RunRow)
                .where(RunRow.id == safe.id)
                .values(status="cancelled", finished_at=datetime.now(UTC))
            )

        unsafe = await database.create_run(
            skill=skill,
            version=version,
            inputs={},
            status="queued",
            idempotency_key="unsafe-stale",
        )
        assert await database.claim_run(unsafe.id, "dead-worker") is not None
        action = await database.start_browser_action(
            recording_id=None,
            run_id=unsafe.id,
            source="replay",
            tool_name="browser_click",
            arguments={"target": "button"},
            upstream_tool_name="browser_click",
            upstream_arguments={"target": "e1"},
            snapshot_before=None,
            durable_locator=None,
        )
        async with database.sessions.begin() as session:
            await session.execute(
                update(RunRow).where(RunRow.id == unsafe.id).values(heartbeat_at=old_heartbeat)
            )

        recovered = await database.recover_stale_runs(60)
        assert recovered == {
            "requeued": [],
            "failed_unknown": [unsafe.id],
            "repair_session_expired": [],
            "approval_session_expired": [],
        }
        unsafe_after = await database.get_run(unsafe.id)
        assert unsafe_after is not None
        assert unsafe_after.status == "failed_unknown"
        assert unsafe_after.failure_context is not None
        assert unsafe_after.failure_context["side_effect_state"] == "unknown"

        async with database.sessions() as session:
            stored_action = await session.scalar(
                select(BrowserActionRow).where(BrowserActionRow.id == action.id)
            )
        assert stored_action is not None
        assert stored_action.state == "unknown"
        assert stored_action.success is None

        waiting = await database.create_run(
            skill=skill,
            version=version,
            inputs={},
            status="queued",
            idempotency_key="stale-approval",
        )
        assert await database.claim_run(waiting.id, "dead-worker") is not None
        approval = await database.get_or_create_approval(
            run_id=waiting.id,
            workflow_version_id=version.id,
            step_index=0,
            gate_fingerprint="a" * 64,
            reason="publish",
            requested_by_principal_id=None,
        )
        async with database.sessions.begin() as session:
            await session.execute(
                update(RunRow)
                .where(RunRow.id == waiting.id)
                .values(status="approval_required", heartbeat_at=old_heartbeat)
            )
        recovered = await database.recover_stale_runs(60)
        assert recovered == {
            "requeued": [],
            "failed_unknown": [],
            "repair_session_expired": [],
            "approval_session_expired": [waiting.id],
        }
        expired = await database.get_approval(approval.id)
        assert expired is not None
        assert expired.status == "session_expired"
    finally:
        await database.close()
