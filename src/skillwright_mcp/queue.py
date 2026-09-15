from __future__ import annotations

import asyncio
import os
import socket
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from taskiq_redis import RedisStreamBroker

from .auth import AuthorizationService
from .browser import BrowserController
from .config import Settings
from .db import Database
from .engine import WorkflowEngine
from .playwright import PlaywrightMCPClient

_worker_settings = Settings()

broker = RedisStreamBroker(
    url=_worker_settings.redis_url,
    queue_name=_worker_settings.redis_queue_name,
    consumer_group_name="skillwright-workers",
)


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid4()}"


@broker.task(task_name="skillwright.execute_run")
async def execute_run_task(run_id: str) -> dict[str, Any]:
    """Execute one persisted run in an isolated Playwright MCP/browser session."""

    settings = Settings()
    database = Database(settings.database_url)
    await database.initialize(create_schema=False)
    authorization = AuthorizationService(database, settings)
    playwright = PlaywrightMCPClient(settings)
    browser = BrowserController(playwright, database)
    engine = WorkflowEngine(database, browser, authorization)
    worker_id = _worker_id()
    try:
        result = await engine.execute_persisted_run(run_id, worker_id=worker_id)
        if result.get("status") != "repair_required":
            return result

        run = await database.get_run(run_id)
        if run is None or run.worker_id != worker_id:
            return {
                "status": "ignored",
                "run_id": run_id,
                "reason": "repair_session_owned_by_other_worker",
            }
        return await _wait_for_repair(
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
            and run.status in {"running", "repair_required"}
            and run.worker_id == worker_id
        ):
            failure = {
                "status": "failed_unknown",
                "run_id": run_id,
                "reason": "worker_exception",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "side_effect_state": "unknown",
            }
            await database.update_run(
                run_id,
                status="failed_unknown",
                failure_context=failure,
                finish=True,
            )
            return failure
        return {
            "status": "ignored",
            "run_id": run_id,
            "reason": "run_not_owned_by_worker",
        }
    finally:
        await playwright.close()
        await database.close()


async def _wait_for_repair(
    *,
    engine: WorkflowEngine,
    database: Database,
    settings: Settings,
    run_id: str,
    worker_id: str,
) -> dict[str, Any]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + settings.repair_wait_timeout_seconds
    while loop.time() < deadline:
        if await database.is_cancel_requested(run_id):
            await database.update_run(
                run_id,
                status="cancelled",
                failure_context=None,
                finish=True,
            )
            return {"status": "cancelled", "run_id": run_id}

        if not await database.heartbeat_repair_session(run_id, worker_id):
            run = await database.get_run(run_id)
            return {
                "status": run.status if run is not None else "not_found",
                "run_id": run_id,
                "reason": "repair_session_lost",
            }

        repair = await database.claim_pending_repair(run_id)
        if repair is not None:
            result = await engine.apply_repair(repair, worker_id=worker_id)
            if result.get("status") == "repair_validation_failed":
                continue
            if result.get("status") != "repair_required":
                return result

        await asyncio.sleep(settings.repair_poll_interval_seconds)

    await database.expire_repair_session(run_id, worker_id=worker_id)
    return {
        "status": "repair_session_expired",
        "run_id": run_id,
        "reason": "repair_wait_timeout",
    }


@dataclass(slots=True)
class RunDispatcher:
    settings: Settings
    database: Database
    engine: WorkflowEngine
    _reaper_task: asyncio.Task[None] | None = None
    _broker_started: bool = False

    async def start(self) -> None:
        if self.settings.execution_backend != "redis" or self._broker_started:
            return
        await broker.startup()
        self._broker_started = True
        self._reaper_task = asyncio.create_task(
            self._reaper_loop(),
            name="skillwright-stale-run-reaper",
        )

    async def close(self) -> None:
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reaper_task
            self._reaper_task = None
        if self._broker_started:
            await broker.shutdown()
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
        if prepared["status"] != "queued":
            return prepared

        run_id = str(prepared["run_id"])
        try:
            task = await execute_run_task.kicker().with_task_id(f"skillwright-run-{run_id}").kiq(
                run_id
            )
        except Exception as exc:
            return {
                **prepared,
                "status": "queue_unavailable",
                "queue_error": f"{type(exc).__name__}: {exc}",
            }
        return {**prepared, "task_id": task.task_id}

    async def cancel(self, run_id: str) -> dict[str, Any]:
        row = await self.database.request_cancel(run_id)
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
    ) -> dict[str, Any]:
        if self.settings.execution_backend == "inline":
            return await self.engine.repair(
                run_id,
                step=step,
                replacement_element_id=replacement_element_id,
                persist=persist,
            )
        return await self.engine.request_repair(
            run_id,
            step=step,
            replacement_element_id=replacement_element_id,
            persist=persist,
        )

    async def recover_stale(self) -> dict[str, list[str]]:
        recovered = await self.database.recover_stale_runs(
            self.settings.run_stale_after_seconds
        )
        if self.settings.execution_backend == "redis":
            for run_id in recovered["requeued"]:
                await execute_run_task.kicker().with_task_id(
                    f"skillwright-run-retry-{run_id}-{uuid4()}"
                ).kiq(run_id)
        return recovered

    async def _reaper_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.stale_reaper_interval_seconds)
            try:
                await self.recover_stale()
            except Exception:
                # A transient Redis/DB outage must not kill the reaper loop.
                continue
