from __future__ import annotations

import asyncio
import hashlib
import os
import socket
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from sqlalchemy import select, update
from taskiq import AsyncTaskiqDecoratedTask
from taskiq_redis import RedisStreamBroker

from .auth import AuthorizationService
from .browser import BrowserController
from .config import Settings
from .db import Database, RunRow
from .engine import WorkflowEngine
from .observability import (
    configure_observability,
    instrument_taskiq_broker,
    record_queue_publish,
)
from .playwright import PlaywrightMCPClient
from .telemetry import stale_recovery_recorded

_worker_settings = Settings()
_EXECUTE_RUN_TASK_NAME = "skillwright.execute_run"


def create_broker(settings: Settings) -> RedisStreamBroker:
    return RedisStreamBroker(
        url=settings.redis_url,
        queue_name=settings.redis_queue_name,
        consumer_group_name="skillwright-workers",
    )


broker = create_broker(_worker_settings)
instrument_taskiq_broker(broker, configure_observability(_worker_settings))


class QueuePublishError(RuntimeError):
    def __init__(self, error_type: str) -> None:
        super().__init__(error_type)
        self.error_type = error_type


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid4()}"


async def execute_run(run_id: str) -> dict[str, Any]:
    """Execute one persisted run in an isolated Playwright MCP/browser session."""

    settings = Settings()
    run_directory_key = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    browser_settings = settings.model_copy(
        update={
            "playwright_output_dir": settings.playwright_output_dir
            / "runs"
            / run_directory_key
        }
    )
    database = Database(settings.database_url)
    await database.initialize(create_schema=False)
    authorization = AuthorizationService(database, settings)
    playwright = PlaywrightMCPClient(browser_settings)
    browser = BrowserController(playwright, database)
    engine = WorkflowEngine(database, browser, authorization)
    worker_id = _worker_id()
    try:
        result = await engine.execute_persisted_run(run_id, worker_id=worker_id)
        if result.get("status") not in {"repair_required", "approval_required"}:
            return result

        run = await database.get_run(run_id)
        if run is None or run.worker_id != worker_id:
            return {
                "status": "ignored",
                "run_id": run_id,
                "reason": "intervention_session_owned_by_other_worker",
            }
        return await _wait_for_intervention(
            engine=engine,
            database=database,
            settings=settings,
            run_id=run_id,
            worker_id=worker_id,
        )
    except Exception as exc:
        run = await database.get_run(run_id)
        if (
            run is not None
            and run.status in {"running", "repair_required", "approval_required"}
            and run.worker_id == worker_id
        ):
            failure = {
                "status": "failed_unknown",
                "run_id": run_id,
                "reason": "worker_exception",
                "error_type": type(exc).__name__,
                "side_effect_state": "unknown",
            }
            updated = await database.update_owned_run(
                run_id,
                worker_id,
                expected_status=run.status,
                status="failed_unknown",
                failure_context=failure,
                finish=True,
            )
            if updated:
                return failure
        return {
            "status": "ignored",
            "run_id": run_id,
            "reason": "run_not_owned_by_worker",
        }
    finally:
        try:
            await browser.close()
            # This marker is written only after the Playwright/MCP context has fully exited. It
            # makes worker teardown observable and lets integration tests prove a terminal run
            # was not reported before the AnyIO-backed stdio session actually closed.
            finished_run = await database.get_run(run_id)
            if finished_run is not None and finished_run.worker_id == worker_id:
                await database.audit(
                    "skill.run.worker_finished",
                    entity_type="run",
                    entity_id=run_id,
                    data={"worker_id": worker_id},
                )
        finally:
            await database.close()


async def _wait_for_intervention(
    *,
    engine: WorkflowEngine,
    database: Database,
    settings: Settings,
    run_id: str,
    worker_id: str,
) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    waiting_status: str | None = None
    deadline: float | None = None
    while True:
        if await database.is_cancel_requested(run_id):
            cancelled = await database.request_cancel(run_id)
            return {
                "status": cancelled.status if cancelled is not None else "not_found",
                "run_id": run_id,
            }

        run = await database.get_run(run_id)
        if run is None:
            return {"status": "not_found", "run_id": run_id}
        if run.status not in {"repair_required", "approval_required"}:
            return {"status": run.status, "run_id": run.id}

        if waiting_status != run.status:
            waiting_status = run.status
            timeout_seconds = (
                settings.repair_wait_timeout_seconds
                if run.status == "repair_required"
                else settings.approval_wait_timeout_seconds
            )
            deadline = loop.time() + timeout_seconds

        if deadline is not None and loop.time() >= deadline:
            if run.status == "approval_required":
                await database.expire_approval_session(run_id, worker_id=worker_id)
                status = "approval_session_expired"
            else:
                await database.expire_repair_session(run_id, worker_id=worker_id)
                status = "repair_session_expired"
            return {
                "status": status,
                "run_id": run_id,
                "reason": "intervention_wait_timeout",
            }

        if not await database.heartbeat_intervention_session(run_id, worker_id):
            latest = await database.get_run(run_id)
            return {
                "status": latest.status if latest is not None else "not_found",
                "run_id": run_id,
                "reason": "intervention_session_lost",
            }

        if run.status == "repair_required":
            repair = await database.claim_pending_repair(run_id)
            if repair is not None:
                result = await engine.apply_repair(repair, worker_id=worker_id)
                if result.get("status") == "repair_validation_failed":
                    continue
                if result.get("status") not in {"repair_required", "approval_required"}:
                    return result
        elif run.status == "approval_required":
            context = run.failure_context or {}
            approval_id = context.get("approval_id")
            if isinstance(approval_id, str):
                approval = await database.get_approval(approval_id)
                if approval is not None and approval.status == "approved":
                    result = await engine.resume_waiting_run(run_id, worker_id=worker_id)
                    if result.get("status") not in {"repair_required", "approval_required"}:
                        return result
        await asyncio.sleep(settings.repair_poll_interval_seconds)


def bind_execute_run_task(
    task_broker: RedisStreamBroker,
) -> AsyncTaskiqDecoratedTask[Any, Any]:
    async def bound_execute_run(run_id: str) -> dict[str, Any]:
        return await execute_run(run_id)

    return task_broker.task(task_name=_EXECUTE_RUN_TASK_NAME)(bound_execute_run)


execute_run_task = bind_execute_run_task(broker)


@dataclass(slots=True)
class RunDispatcher:
    settings: Settings
    database: Database
    engine: WorkflowEngine
    publisher_broker: RedisStreamBroker | None = None
    _reaper_task: asyncio.Task[None] | None = None
    _broker_started: bool = False
    _publish_task: AsyncTaskiqDecoratedTask[Any, Any] | None = field(
        init=False,
        default=None,
        repr=False,
    )

    def __post_init__(self) -> None:
        if self.settings.execution_backend != "redis":
            return
        if self.publisher_broker is None:
            self.publisher_broker = create_broker(self.settings)
        observability = configure_observability(self.settings)
        instrument_taskiq_broker(self.publisher_broker, observability)
        self._publish_task = bind_execute_run_task(self.publisher_broker)

    async def start(self) -> None:
        if self.settings.execution_backend != "redis":
            return
        if self.publisher_broker is None:
            raise RuntimeError("redis dispatcher has no publisher broker")
        if self._reaper_task is None:
            self._reaper_task = asyncio.create_task(
                self._reaper_loop(),
                name="skillwright-stale-run-reaper",
            )
        if not self._broker_started:
            await self.publisher_broker.startup()
            self._broker_started = True

    async def close(self) -> None:
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reaper_task
            self._reaper_task = None
        if self._broker_started:
            if self.publisher_broker is None:
                raise RuntimeError("redis dispatcher lost its publisher broker")
            await self.publisher_broker.shutdown()
            self._broker_started = False

    async def submit(
        self,
        name: str,
        *,
        inputs: dict[str, Any] | None = None,
        version: int | None = None,
        idempotency_key: str | None = None,
        requested_by_principal_id: str | None = None,
    ) -> dict[str, Any]:
        if self.settings.execution_backend == "inline":
            return await self.engine.run_skill(
                name,
                inputs=inputs,
                version=version,
                idempotency_key=idempotency_key,
                requested_by_principal_id=requested_by_principal_id,
            )

        prepared = await self.engine.prepare_run(
            name,
            inputs=inputs,
            version=version,
            idempotency_key=idempotency_key,
            requested_by_principal_id=requested_by_principal_id,
        )
        if prepared["status"] not in {"queued", "retrying"}:
            return prepared

        run_id = str(prepared["run_id"])
        try:
            task_id = await self._publish_run(
                run_id,
                recovered=prepared["status"] == "retrying",
            )
        except QueuePublishError as exc:
            return {
                **prepared,
                "status": "queue_unavailable",
                "queue_error_type": exc.error_type,
            }
        return {
            **prepared,
            "status": "queued",
            "failure_context": None,
            "task_id": task_id,
        }

    async def _send_task(self, run_id: str) -> str:
        if self._publish_task is None:
            raise RuntimeError("redis dispatcher has no publisher task")
        task = await self._publish_task.kicker().with_task_id(f"skillwright-run-{run_id}").kiq(
            run_id
        )
        return task.task_id

    async def _publish_run(self, run_id: str, *, recovered: bool) -> str:
        try:
            task_id = await self._send_task(run_id)
        except Exception as exc:
            error_type = type(exc).__name__
            await self._mark_publish_failed(run_id, error_type)
            record_queue_publish("failure")
            raise QueuePublishError(error_type) from exc

        if recovered:
            await self._clear_publish_failure(run_id)
            record_queue_publish("recovered")
        else:
            record_queue_publish("success")
        return task_id

    async def _mark_publish_failed(self, run_id: str, error_type: str) -> None:
        failure = {
            "status": "queue_unavailable",
            "reason": "queue_publish_failed",
            "error_type": error_type,
            "side_effect_state": "not_started",
        }
        async with self.database.sessions.begin() as session:
            await session.execute(
                update(RunRow)
                .where(
                    RunRow.id == run_id,
                    RunRow.status.in_(["queued", "retrying"]),
                    RunRow.worker_id.is_(None),
                )
                .values(status="retrying", failure_context=failure)
            )

    async def _clear_publish_failure(self, run_id: str) -> None:
        async with self.database.sessions.begin() as session:
            await session.execute(
                update(RunRow)
                .where(
                    RunRow.id == run_id,
                    RunRow.status == "retrying",
                    RunRow.worker_id.is_(None),
                )
                .values(status="queued", failure_context=None)
            )

    async def recover_publish_failures(self, *, limit: int = 100) -> list[str]:
        async with self.database.sessions() as session:
            run_ids = list(
                (
                    await session.scalars(
                        select(RunRow.id)
                        .where(
                            RunRow.status == "retrying",
                            RunRow.worker_id.is_(None),
                            RunRow.cancel_requested.is_(False),
                        )
                        .order_by(RunRow.queued_at.asc())
                        .limit(limit)
                    )
                ).all()
            )

        recovered: list[str] = []
        for run_id in run_ids:
            try:
                await self._publish_run(run_id, recovered=True)
            except QueuePublishError:
                continue
            recovered.append(run_id)
        return recovered

    async def cancel(
        self,
        run_id: str,
        *,
        actor_principal_id: str | None = None,
    ) -> dict[str, Any]:
        row = await self.database.request_cancel(
            run_id,
            actor_principal_id=actor_principal_id,
        )
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
        if self.settings.execution_backend == "inline":
            return await self.engine.repair(
                run_id,
                step=step,
                replacement_element_id=replacement_element_id,
                persist=persist,
                actor_principal_id=actor_principal_id,
            )
        return await self.engine.request_repair(
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
            approval = await self.database.decide_approval(
                approval_id,
                approve=approve,
                decided_by_principal_id=decided_by_principal_id,
                comment=comment,
            )
        except (KeyError, ValueError) as exc:
            return {"status": "invalid_approval", "approval_id": approval_id, "error": str(exc)}
        if not approve:
            return {
                "status": "rejected",
                "approval_id": approval.id,
                "run_id": approval.run_id,
            }
        if self.settings.execution_backend == "inline":
            return await self.engine.resume_waiting_run(approval.run_id)
        return {
            "status": "approved",
            "approval_id": approval.id,
            "run_id": approval.run_id,
        }

    async def recover_stale(self) -> dict[str, list[str]]:
        recovered = await self.database.recover_stale_runs(
            self.settings.run_stale_after_seconds
        )
        for outcome, run_ids in recovered.items():
            stale_recovery_recorded(outcome=outcome, count=len(run_ids))
        if self.settings.execution_backend == "redis":
            for run_id in recovered["requeued"]:
                try:
                    await self._publish_run(run_id, recovered=True)
                except QueuePublishError:
                    continue
        return recovered

    async def _reaper_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.stale_reaper_interval_seconds)
            try:
                await self.recover_publish_failures()
                await self.recover_stale()
            except Exception:
                # A transient Redis/DB outage must not kill the reaper loop.
                continue
