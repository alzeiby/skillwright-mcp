from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import select, update

import skillwright_mcp.engine as engine_module
from skillwright_mcp.browser import BrowserController
from skillwright_mcp.config import Settings
from skillwright_mcp.db import BrowserActionRow, Database, RepairRow, RunRow
from skillwright_mcp.engine import WorkflowEngine
from skillwright_mcp.playwright import BrowserResult
from skillwright_mcp.queue import _wait_for_intervention
from skillwright_mcp.workflow import WorkflowDefinition


class SlowWaitPlaywright:
    def __init__(self, *, wait_seconds: float = 0.2) -> None:
        self.calls: list[str] = []
        self.wait_seconds = wait_seconds

    async def has_tool(self, _tool_name: str) -> bool:
        return False

    async def call(
        self,
        tool_name: str,
        _arguments: dict[str, Any] | None = None,
    ) -> BrowserResult:
        self.calls.append(tool_name)
        if tool_name == "browser_wait_for":
            await asyncio.sleep(self.wait_seconds)
            text = "waited"
        elif tool_name == "browser_navigate":
            text = "navigated"
        else:
            raise AssertionError(f"unexpected tool {tool_name}")
        return BrowserResult(
            tool_name=tool_name,
            ok=True,
            text=text,
            structured_content=None,
            raw={"text": text},
        )


class PausedClickPlaywright:
    def __init__(self) -> None:
        self.click_started = asyncio.Event()
        self.release_click = asyncio.Event()

    async def has_tool(self, _tool_name: str) -> bool:
        return False

    async def call(
        self,
        tool_name: str,
        _arguments: dict[str, Any] | None = None,
    ) -> BrowserResult:
        if tool_name == "browser_snapshot":
            text = "\n".join(
                [
                    "- Page URL: https://example.test/",
                    "- Page Title: Fixture",
                    '- button "Submit" [ref=e1]',
                ]
            )
        elif tool_name == "browser_click":
            self.click_started.set()
            await self.release_click.wait()
            text = "clicked"
        else:
            raise AssertionError(f"unexpected tool {tool_name}")
        return BrowserResult(
            tool_name=tool_name,
            ok=True,
            text=text,
            structured_content=None,
            raw={"text": text},
        )


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
async def test_idempotency_reuse_requires_same_requester_version_and_inputs(tmp_path: Path) -> None:
    database, skill, version = await _database_with_skill(tmp_path)
    first_requester = await database.ensure_principal("first@example.test")
    second_requester = await database.ensure_principal("second@example.test")
    try:
        first = await database.create_run(
            skill=skill,
            version=version,
            inputs={"request": "same"},
            status="queued",
            idempotency_key="request-key",
            requested_by_principal_id=first_requester.id,
        )
        exact = await database.create_run(
            skill=skill,
            version=version,
            inputs={"request": "same"},
            status="queued",
            idempotency_key="request-key",
            requested_by_principal_id=first_requester.id,
        )
        assert exact.id == first.id

        with pytest.raises(ValueError, match="idempotency key"):
            await database.create_run(
                skill=skill,
                version=version,
                inputs={"request": "same"},
                status="queued",
                idempotency_key="request-key",
                requested_by_principal_id=second_requester.id,
            )
        with pytest.raises(ValueError, match="idempotency key"):
            await database.create_run(
                skill=skill,
                version=version,
                inputs={"request": "different"},
                status="queued",
                idempotency_key="request-key",
                requested_by_principal_id=first_requester.id,
            )

        _, second_version = await database.create_skill_version(
            WorkflowDefinition.model_validate(version.definition),
            expected_current_version=version.version,
        )
        with pytest.raises(ValueError, match="idempotency key"):
            await database.create_run(
                skill=skill,
                version=second_version,
                inputs={"request": "same"},
                status="queued",
                idempotency_key="request-key",
                requested_by_principal_id=first_requester.id,
            )
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_idempotency_conflict_never_returns_other_requesters_run_data(tmp_path: Path) -> None:
    database, skill, _version = await _database_with_skill(tmp_path)
    owner = await database.ensure_principal("owner@example.test")
    runner = await database.ensure_principal("runner@example.test")
    engine = WorkflowEngine(database, cast(Any, object()))
    try:
        first = await engine.prepare_run(
            skill.name,
            idempotency_key="shared-looking-key",
            requested_by_principal_id=owner.id,
        )
        assert first["status"] == "queued"
        await database.update_run(
            str(first["run_id"]),
            outputs={"token": "OWNER_OUTPUT"},
            failure_context={"detail": "OWNER_CONTEXT"},
        )

        conflict = await engine.prepare_run(
            skill.name,
            idempotency_key="shared-looking-key",
            requested_by_principal_id=runner.id,
        )
        assert conflict == {
            "status": "idempotency_conflict",
            "workflow": skill.name,
        }
        serialized = str(conflict)
        assert "OWNER_OUTPUT" not in serialized
        assert "OWNER_CONTEXT" not in serialized
        assert "run_id" not in conflict
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
            "cancelled": [],
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
            "cancelled": [],
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
            "cancelled": [],
            "repair_session_expired": [],
            "approval_session_expired": [waiting.id],
        }
        expired = await database.get_approval(approval.id)
        assert expired is not None
        assert expired.status == "session_expired"
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("action_state", ["succeeded", "failed"])
async def test_stale_run_never_replays_dispatched_mutation_without_step_checkpoint(
    tmp_path: Path,
    action_state: str,
) -> None:
    database, skill, version = await _database_with_skill(tmp_path)
    old_heartbeat = datetime.now(UTC) - timedelta(minutes=30)
    try:
        run = await database.create_run(
            skill=skill,
            version=version,
            inputs={},
            status="queued",
        )
        assert await database.claim_run(run.id, "dead-worker") is not None
        action = await database.start_browser_action(
            recording_id=None,
            run_id=run.id,
            source="replay",
            tool_name="browser_click",
            arguments={"target": "button"},
            upstream_tool_name="browser_click",
            upstream_arguments={"target": "e1"},
            snapshot_before=None,
            durable_locator=None,
        )
        await database.finish_browser_action(
            action.id,
            result={"text": action_state},
            success=action_state == "succeeded",
            error=None if action_state == "succeeded" else "click failed after dispatch",
            snapshot_after=None,
            duration_ms=1.0,
        )
        async with database.sessions.begin() as session:
            await session.execute(
                update(RunRow).where(RunRow.id == run.id).values(heartbeat_at=old_heartbeat)
            )

        recovered = await database.recover_stale_runs(60)
        assert recovered["requeued"] == []
        assert recovered["failed_unknown"] == [run.id]
        stored = await database.get_run(run.id)
        assert stored is not None
        assert stored.status == "failed_unknown"
        assert stored.failure_context is not None
        assert stored.failure_context["side_effect_state"] == "unknown"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_stale_cancelled_running_run_finishes_instead_of_requeueing(tmp_path: Path) -> None:
    database, skill, version = await _database_with_skill(tmp_path)
    old_heartbeat = datetime.now(UTC) - timedelta(minutes=30)
    try:
        run = await database.create_run(
            skill=skill,
            version=version,
            inputs={},
            status="queued",
        )
        assert await database.claim_run(run.id, "dead-worker") is not None
        cancelling = await database.request_cancel(run.id)
        assert cancelling is not None
        assert cancelling.status == "running"
        assert cancelling.cancel_requested is True
        async with database.sessions.begin() as session:
            await session.execute(
                update(RunRow).where(RunRow.id == run.id).values(heartbeat_at=old_heartbeat)
            )

        recovered = await database.recover_stale_runs(60)
        assert recovered["requeued"] == []
        assert recovered["failed_unknown"] == []
        assert recovered["cancelled"] == [run.id]

        stored = await database.get_run(run.id)
        assert stored is not None
        assert stored.status == "cancelled"
        assert stored.finished_at is not None
        assert stored.worker_id is None
        assert await database.claim_run(run.id, "replacement-worker") is None
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_continuous_heartbeat_covers_long_step_and_prevents_stale_requeue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'heartbeat-long-step.db').as_posix()}")
    await database.initialize(create_schema=True)
    workflow = WorkflowDefinition.model_validate(
        {
            "name": "long-wait",
            "steps": [{"op": "wait", "seconds": 0.2}],
        }
    )
    skill, version = await database.create_skill_version(workflow)
    run = await database.create_run(skill=skill, version=version, inputs={}, status="queued")
    fake = SlowWaitPlaywright(wait_seconds=0.8)
    engine = WorkflowEngine(database, BrowserController(cast(Any, fake), database))
    monkeypatch.setattr(engine_module, "RUN_HEARTBEAT_INTERVAL_SECONDS", 0.02)
    original_heartbeat = database.heartbeat_run
    heartbeat_times: asyncio.Queue[float] = asyncio.Queue()

    async def observed_heartbeat(run_id: str, worker_id: str) -> bool:
        alive = await original_heartbeat(run_id, worker_id)
        await heartbeat_times.put(asyncio.get_running_loop().time())
        return alive

    monkeypatch.setattr(database, "heartbeat_run", observed_heartbeat)
    task = asyncio.create_task(engine.execute_persisted_run(run.id, worker_id="worker-long"))
    try:
        # Let the run become older than the stale window, then require a heartbeat committed
        # after that point. This proves the continuous heartbeat?not merely the initial claim?
        # keeps a long browser step alive without depending on sub-SQLite-IO timing.
        await asyncio.sleep(0.35)
        while not heartbeat_times.empty():
            heartbeat_times.get_nowait()
        await asyncio.wait_for(heartbeat_times.get(), timeout=0.5)
        recovered = await database.recover_stale_runs(0.25)
        assert run.id not in recovered["requeued"]
        assert run.id not in recovered["failed_unknown"]
        result = await task
        assert result["status"] == "succeeded"
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await database.close()


@pytest.mark.asyncio
async def test_worker_aborts_long_step_when_run_ownership_is_lost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'heartbeat-ownership.db').as_posix()}")
    await database.initialize(create_schema=True)
    workflow = WorkflowDefinition.model_validate(
        {
            "name": "ownership-loss",
            "steps": [
                {"op": "wait", "seconds": 0.2},
                {"op": "navigate", "url": "https://must-not-run.test"},
            ],
        }
    )
    skill, version = await database.create_skill_version(workflow)
    run = await database.create_run(skill=skill, version=version, inputs={}, status="queued")
    fake = SlowWaitPlaywright(wait_seconds=0.5)
    engine = WorkflowEngine(database, BrowserController(cast(Any, fake), database))
    monkeypatch.setattr(engine_module, "RUN_HEARTBEAT_INTERVAL_SECONDS", 0.01)
    original_heartbeat = database.heartbeat_run
    ownership_injected = asyncio.Event()

    async def heartbeat_with_ownership_loss(run_id: str, worker_id: str) -> bool:
        current = asyncio.current_task()
        if (
            current is not None
            and current.get_name().startswith("skillwright-run-heartbeat-")
            and not ownership_injected.is_set()
        ):
            async with database.sessions.begin() as session:
                updated_id = await session.scalar(
                    update(RunRow)
                    .where(
                        RunRow.id == run_id,
                        RunRow.status == "running",
                        RunRow.worker_id == worker_id,
                    )
                    .values(worker_id="replacement-worker")
                    .returning(RunRow.id)
                )
            assert updated_id == run_id
            ownership_injected.set()
        return await original_heartbeat(run_id, worker_id)

    monkeypatch.setattr(database, "heartbeat_run", heartbeat_with_ownership_loss)
    task = asyncio.create_task(engine.execute_persisted_run(run.id, worker_id="worker-original"))
    try:
        result = await asyncio.wait_for(task, timeout=1.0)
        assert ownership_injected.is_set()
        assert result == {
            "status": "ignored",
            "run_id": run.id,
            "reason": "run_ownership_lost",
        }
        assert "browser_navigate" not in fake.calls
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await database.close()


@pytest.mark.asyncio
async def test_stale_mutating_owner_cannot_resurrect_failed_unknown_run(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'stale-click-owner.db').as_posix()}")
    await database.initialize(create_schema=True)
    workflow = WorkflowDefinition.model_validate(
        {
            "name": "stale-click-owner",
            "steps": [
                {
                    "op": "click",
                    "target": {"role": "button", "name": "Submit"},
                }
            ],
        }
    )
    skill, version = await database.create_skill_version(workflow)
    run = await database.create_run(skill=skill, version=version, inputs={}, status="queued")
    fake = PausedClickPlaywright()
    engine = WorkflowEngine(database, BrowserController(cast(Any, fake), database))
    task = asyncio.create_task(engine.execute_persisted_run(run.id, worker_id="stale-owner"))
    try:
        await asyncio.wait_for(fake.click_started.wait(), timeout=1)
        recovered = await database.recover_stale_runs(0)
        assert recovered["failed_unknown"] == [run.id]
        fenced = await database.get_run(run.id)
        assert fenced is not None
        assert fenced.status == "failed_unknown"
        assert fenced.worker_id is None

        fake.release_click.set()
        result = await asyncio.wait_for(task, timeout=1)
        assert result == {
            "status": "ignored",
            "run_id": run.id,
            "reason": "run_ownership_lost",
        }
        final = await database.get_run(run.id)
        assert final is not None
        assert final.status == "failed_unknown"
        assert final.worker_id is None
    finally:
        fake.release_click.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("waiting_status", "expected_status"),
    [
        ("repair_required", "repair_session_expired"),
        ("approval_required", "approval_session_expired"),
    ],
)
async def test_intervention_wait_uses_timeout_for_current_state(
    tmp_path: Path,
    waiting_status: str,
    expected_status: str,
) -> None:
    database, skill, version = await _database_with_skill(tmp_path)
    worker_id = "waiting-worker"
    settings = Settings(
        database_url=database.url,
        execution_backend="inline",
    )
    # Assignment validation is intentionally disabled by Settings; tiny values keep this unit
    # test fast while proving the waiter does not use max(repair_timeout, approval_timeout).
    settings.repair_poll_interval_seconds = 0.001
    if waiting_status == "repair_required":
        settings.repair_wait_timeout_seconds = 0.01
        settings.approval_wait_timeout_seconds = 5
    else:
        settings.repair_wait_timeout_seconds = 5
        settings.approval_wait_timeout_seconds = 0.01

    try:
        run = await database.create_run(
            skill=skill,
            version=version,
            inputs={},
            status="queued",
        )
        assert await database.claim_run(run.id, worker_id) is not None
        failure_context: dict[str, Any] = {
            "status": waiting_status,
            "run_id": run.id,
            "session_available": True,
        }
        if waiting_status == "approval_required":
            approval = await database.get_or_create_approval(
                run_id=run.id,
                workflow_version_id=version.id,
                step_index=0,
                gate_fingerprint="b" * 64,
                reason="external mutation",
                requested_by_principal_id=None,
            )
            failure_context["approval_id"] = approval.id
        await database.update_run(
            run.id,
            status=waiting_status,
            failure_context=failure_context,
        )

        result = await asyncio.wait_for(
            _wait_for_intervention(
                engine=cast(Any, object()),
                database=database,
                settings=settings,
                run_id=run.id,
                worker_id=worker_id,
            ),
            timeout=0.5,
        )
        assert result["status"] == expected_status

        stored = await database.get_run(run.id)
        assert stored is not None
        assert stored.status == expected_status
        assert stored.failure_context is not None
        assert stored.failure_context["session_available"] is False
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_cancelled_repair_cannot_be_overwritten_by_stale_worker_completion(
    tmp_path: Path,
) -> None:
    database, skill, version = await _database_with_skill(tmp_path)
    try:
        run = await database.create_run(
            skill=skill,
            version=version,
            inputs={},
            status="queued",
        )
        assert await database.claim_run(run.id, "repair-worker") is not None
        await database.update_run(
            run.id,
            status="repair_required",
            failure_context={
                "status": "repair_required",
                "run_id": run.id,
                "step": 0,
                "session_available": True,
            },
        )
        repair, created = await database.create_repair(
            run_id=run.id,
            workflow_version_id=version.id,
            step_index=0,
            expected_target={"role": "button", "name": "Old"},
            replacement_target={"role": "button", "name": "New"},
            candidate_id="candidate-0",
        )
        assert created is True
        claimed = await database.claim_pending_repair(run.id)
        assert claimed is not None
        assert claimed.status == "applying"

        cancelled = await database.request_cancel(run.id)
        assert cancelled is not None
        assert cancelled.status == "cancelled"
        after_cancel = await database.get_repair(repair.id)
        assert after_cancel is not None
        assert after_cancel.status == "cancelled"

        await database.complete_repair(
            repair.id,
            status="run_incomplete",
            validation_result={"status": "cancelled", "run_id": run.id},
        )
        final = await database.get_repair(repair.id)
        assert final is not None
        assert final.status == "cancelled"
        assert final.completed_at is not None
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_concurrent_repair_proposals_leave_one_active_repair(tmp_path: Path) -> None:
    database, skill, version = await _database_with_skill(tmp_path)
    try:
        run = await database.create_run(
            skill=skill,
            version=version,
            inputs={},
            status="queued",
        )
        assert await database.claim_run(run.id, "repair-worker") is not None
        await database.update_run(
            run.id,
            status="repair_required",
            failure_context={
                "status": "repair_required",
                "run_id": run.id,
                "step": 0,
                "session_available": True,
            },
        )

        first, second = await asyncio.gather(
            database.create_repair(
                run_id=run.id,
                workflow_version_id=version.id,
                step_index=0,
                expected_target={"role": "button", "name": "Old"},
                replacement_target={"role": "button", "name": "First"},
                candidate_id="candidate-first",
            ),
            database.create_repair(
                run_id=run.id,
                workflow_version_id=version.id,
                step_index=0,
                expected_target={"role": "button", "name": "Old"},
                replacement_target={"role": "button", "name": "Second"},
                candidate_id="candidate-second",
            ),
        )
        assert sorted([first[1], second[1]]) == [False, True]
        assert first[0].id == second[0].id

        async with database.sessions() as session:
            active = (
                await session.scalars(
                    select(RepairRow).where(
                        RepairRow.run_id == run.id,
                        RepairRow.status.in_(["pending", "applying"]),
                    )
                )
            ).all()
        assert len(active) == 1
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_repair_creation_rechecks_terminal_parent_run(tmp_path: Path) -> None:
    database, skill, version = await _database_with_skill(tmp_path)
    try:
        run = await database.create_run(
            skill=skill,
            version=version,
            inputs={},
            status="queued",
        )
        assert await database.claim_run(run.id, "repair-worker") is not None
        await database.update_run(
            run.id,
            status="repair_required",
            failure_context={
                "status": "repair_required",
                "run_id": run.id,
                "step": 0,
                "session_available": True,
            },
        )
        cancelled = await database.request_cancel(run.id)
        assert cancelled is not None
        assert cancelled.status == "cancelled"

        with pytest.raises(ValueError, match="not awaiting repair"):
            await database.create_repair(
                run_id=run.id,
                workflow_version_id=version.id,
                step_index=0,
                expected_target={"role": "button", "name": "Old"},
                replacement_target={"role": "button", "name": "New"},
                candidate_id="candidate-late",
            )
        assert await database.pending_repair(run.id) is None
    finally:
        await database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("waiting_status", ["repair_required", "approval_required"])
async def test_intervention_wait_persists_cancel_that_raced_running_step(
    tmp_path: Path,
    waiting_status: str,
) -> None:
    database, skill, version = await _database_with_skill(tmp_path)
    worker_id = "cancel-race-worker"
    settings = Settings(database_url=database.url, execution_backend="inline")
    try:
        run = await database.create_run(
            skill=skill,
            version=version,
            inputs={},
            status="queued",
        )
        assert await database.claim_run(run.id, worker_id) is not None

        # Cancellation while a browser step is already in flight only sets the flag because
        # the run is still `running`. Reproduce the old race by then persisting the step's
        # intervention result before the retained worker notices the flag.
        cancelling = await database.request_cancel(run.id)
        assert cancelling is not None
        assert cancelling.status == "running"
        assert cancelling.cancel_requested is True

        failure_context: dict[str, Any] = {
            "status": waiting_status,
            "run_id": run.id,
            "step": 0,
            "session_available": True,
        }
        if waiting_status == "approval_required":
            approval = await database.get_or_create_approval(
                run_id=run.id,
                workflow_version_id=version.id,
                step_index=0,
                gate_fingerprint="c" * 64,
                reason="external mutation",
                requested_by_principal_id=None,
            )
            failure_context["approval_id"] = approval.id
        await database.update_run(
            run.id,
            status=waiting_status,
            failure_context=failure_context,
        )

        result = await _wait_for_intervention(
            engine=cast(Any, object()),
            database=database,
            settings=settings,
            run_id=run.id,
            worker_id=worker_id,
        )
        assert result == {"status": "cancelled", "run_id": run.id}

        stored = await database.get_run(run.id)
        assert stored is not None
        assert stored.status == "cancelled"
        assert stored.finished_at is not None
        assert stored.worker_id is None
        if waiting_status == "approval_required":
            stored_approval = await database.get_approval(str(failure_context["approval_id"]))
            assert stored_approval is not None
            assert stored_approval.status == "cancelled"
    finally:
        await database.close()
