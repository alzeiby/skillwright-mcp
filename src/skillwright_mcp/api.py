from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from ipaddress import ip_address
from typing import Any, cast

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import __version__
from .auth import AuthorizationError, SkillPermission
from .config import Settings
from .db import PrincipalRow, RunRow, SkillRow
from .observability import configure_observability, instrument_fastapi
from .runtime import Runtime, build_runtime

logger = logging.getLogger(__name__)

PrincipalResolver = Callable[[Request, Runtime], Awaitable[PrincipalRow]]
RuntimeFactory = Callable[[Settings], Runtime]

_TERMINAL_RUN_STATUSES = {
    "succeeded",
    "failed",
    "failed_unknown",
    "cancelled",
    "rejected",
    "repair_session_expired",
    "approval_session_expired",
}


class RunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill: str = Field(min_length=1, max_length=200)
    inputs: dict[str, Any] = Field(default_factory=dict)
    version: int | None = Field(default=None, ge=1)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=160)


class RunStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: str
    workflow_version: int
    current_step: int
    cancel_requested: bool
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class RepairRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step: int = Field(ge=0)
    replacement_element_id: str = Field(min_length=1, max_length=80)
    persist: bool = True


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approve: bool
    comment: str | None = Field(default=None, max_length=2_000)


def _runtime(request: Request) -> Runtime:
    return cast(Runtime, request.app.state.runtime)


def _is_loopback(request: Request) -> bool:
    if request.client is None:
        return False
    try:
        return ip_address(request.client.host).is_loopback
    except ValueError:
        return request.client.host == "localhost"


async def _default_principal_resolver(request: Request, runtime: Runtime) -> PrincipalRow:
    authorization_header = request.headers.get("authorization")
    if authorization_header is not None:
        scheme, separator, token = authorization_header.partition(" ")
        if separator and scheme.casefold() == "bearer" and token:
            external_key = runtime.bearer_auth.principal_for_token(token)
            if external_key is not None:
                return await runtime.authorization.authenticated_principal(external_key)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_token"},
        )
    if runtime.settings.allow_unauthenticated_local and _is_loopback(request):
        return await runtime.authorization.local_principal()
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "authentication_required"},
    )


def _run_status(row: RunRow) -> RunStatus:
    return RunStatus(
        run_id=row.id,
        status=row.status,
        workflow_version=row.workflow_version,
        current_step=row.current_step,
        cancel_requested=row.cancel_requested,
        queued_at=row.queued_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


async def _authorized_skill(
    runtime: Runtime,
    principal: PrincipalRow,
    name: str,
    permission: SkillPermission,
) -> SkillRow:
    skill = await runtime.database.get_skill(name)
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "not_found"})
    await runtime.authorization.require_skill(principal, skill, permission)
    return skill


async def _authorized_run(
    runtime: Runtime,
    principal: PrincipalRow,
    run_id: str,
    permission: SkillPermission,
) -> RunRow:
    run = await runtime.database.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "not_found"})
    skill = await runtime.database.get_skill_by_id(run.skill_id)
    if skill is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "not_found"})
    await runtime.authorization.require_skill(principal, skill, permission)
    return run


def _intervention_context(run: RunRow) -> dict[str, Any] | None:
    context = run.failure_context or {}
    if run.status == "repair_required":
        return {
            "type": "repair",
            "run_id": run.id,
            "status": run.status,
            "step": context.get("step"),
            "operation": context.get("operation"),
            "expected": context.get("expected"),
            "candidates": context.get("candidates", []),
            "session_available": context.get("session_available", False),
        }
    if run.status == "approval_required":
        return {
            "type": "approval",
            "run_id": run.id,
            "status": run.status,
            "step": context.get("step"),
            "operation": context.get("operation"),
            "approval_id": context.get("approval_id"),
            "reason": context.get("reason"),
            "target": context.get("target"),
            "session_available": context.get("session_available", False),
        }
    return None


def create_app(
    settings: Settings | None = None,
    *,
    runtime_factory: RuntimeFactory = build_runtime,
    principal_resolver: PrincipalResolver | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings()
    resolve_principal = principal_resolver or _default_principal_resolver
    observability = configure_observability(resolved_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runtime = runtime_factory(resolved_settings)
        app.state.runtime = runtime
        try:
            try:
                async with asyncio.timeout(resolved_settings.healthcheck_timeout_seconds):
                    await runtime.start()
            except Exception as exc:
                logger.warning(
                    "control API started before dependencies were ready (%s)",
                    type(exc).__name__,
                )
            yield
        finally:
            await runtime.close()

    api = FastAPI(
        title="Skillwright Control API",
        version=__version__,
        lifespan=lifespan,
    )

    @api.exception_handler(AuthorizationError)
    async def authorization_error(_: Request, exc: AuthorizationError) -> JSONResponse:
        status_code = (
            status.HTTP_401_UNAUTHORIZED
            if exc.code in {"authentication_required", "unknown_principal"}
            else status.HTTP_403_FORBIDDEN
        )
        return JSONResponse(status_code=status_code, content={"detail": {"code": exc.code}})

    @api.get("/health/live")
    async def health_live() -> dict[str, str]:
        return {"status": "live"}

    @api.get("/health/ready")
    async def health_ready(request: Request) -> JSONResponse:
        checks = await _runtime(request).readiness()
        ready = all(value == "ok" for value in checks.values())
        return JSONResponse(
            status_code=status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "ready" if ready else "not_ready", "checks": checks},
        )

    @api.post("/api/v1/runs", response_model=RunStatus)
    async def create_run(payload: RunCreate, request: Request, response: Response) -> RunStatus:
        runtime = _runtime(request)
        principal = await resolve_principal(request, runtime)
        await _authorized_skill(runtime, principal, payload.skill, "run")
        result = await runtime.dispatcher.submit(
            payload.skill,
            inputs=payload.inputs,
            version=payload.version,
            idempotency_key=payload.idempotency_key,
            requested_by_principal_id=principal.id,
        )
        result_status = str(result.get("status"))
        if result_status == "not_found":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "not_found"},
            )
        if result_status == "invalid_inputs":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": "invalid_inputs", "error": result.get("error")},
            )
        if result_status == "secret_unavailable":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"code": "secret_unavailable"},
            )
        if result_status == "idempotency_conflict":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "idempotency_conflict"},
            )
        if result_status == "queue_unavailable":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "queue_unavailable",
                    "run_id": result.get("run_id"),
                    "error_type": result.get("queue_error_type"),
                },
            )

        run_id = result.get("run_id")
        if not isinstance(run_id, str):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"code": "run_state_invalid"},
            )
        run = await runtime.database.get_run(run_id)
        if run is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"code": "run_state_missing"},
            )
        response.status_code = (
            status.HTTP_202_ACCEPTED if result_status == "queued" else status.HTTP_200_OK
        )
        return _run_status(run)

    @api.get("/api/v1/runs/{run_id}", response_model=RunStatus)
    async def get_run(run_id: str, request: Request) -> RunStatus:
        runtime = _runtime(request)
        principal = await resolve_principal(request, runtime)
        run = await _authorized_run(runtime, principal, run_id, "view")
        return _run_status(run)

    @api.post("/api/v1/runs/{run_id}/cancel", response_model=RunStatus)
    async def cancel_run(run_id: str, request: Request, response: Response) -> RunStatus:
        runtime = _runtime(request)
        principal = await resolve_principal(request, runtime)
        before = await _authorized_run(runtime, principal, run_id, "run")
        result = await runtime.dispatcher.cancel(
            run_id,
            actor_principal_id=principal.id,
        )
        if result["status"] == "not_found":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "not_found"},
            )
        run = await runtime.database.get_run(run_id)
        if run is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"code": "run_state_missing"},
            )
        newly_requested = not before.cancel_requested and run.cancel_requested
        response.status_code = (
            status.HTTP_202_ACCEPTED
            if newly_requested and run.status not in _TERMINAL_RUN_STATUSES
            else status.HTTP_200_OK
        )
        return _run_status(run)

    @api.get("/api/v1/runs/{run_id}/intervention")
    async def get_intervention(run_id: str, request: Request) -> dict[str, Any]:
        runtime = _runtime(request)
        principal = await resolve_principal(request, runtime)
        run = await runtime.database.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "not_found"})
        skill = await runtime.database.get_skill_by_id(run.skill_id)
        if skill is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "not_found"})
        if run.status == "repair_required":
            permission: SkillPermission = "edit"
        elif run.status == "approval_required":
            permission = "approve"
        else:
            permission = "view"
        await runtime.authorization.require_skill(principal, skill, permission)
        context = _intervention_context(run)
        if context is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "no_intervention_required", "status": run.status},
            )
        return context

    @api.post("/api/v1/runs/{run_id}/repair")
    async def repair_run(
        run_id: str,
        payload: RepairRequest,
        request: Request,
    ) -> dict[str, Any]:
        runtime = _runtime(request)
        principal = await resolve_principal(request, runtime)
        await _authorized_run(runtime, principal, run_id, "edit")
        result = await runtime.dispatcher.repair(
            run_id,
            step=payload.step,
            replacement_element_id=payload.replacement_element_id,
            persist=payload.persist,
            actor_principal_id=principal.id,
        )
        result_status = str(result.get("status"))
        if result_status == "not_found":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "not_found"})
        if result_status in {"not_repairable", "repair_conflict", "repair_session_mismatch"}:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": result_status},
            )
        if result_status in {"invalid_repair", "repair_validation_failed"}:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": result_status},
            )
        return {
            key: value
            for key, value in result.items()
            if key
            in {
                "status",
                "run_id",
                "repair_id",
                "step",
                "replacement_element_id",
                "saved_workflow_version",
            }
        }

    @api.post("/api/v1/approvals/{approval_id}/decision")
    async def decide_approval(
        approval_id: str,
        payload: ApprovalDecision,
        request: Request,
    ) -> dict[str, Any]:
        runtime = _runtime(request)
        principal = await resolve_principal(request, runtime)
        approval = await runtime.database.get_approval(approval_id)
        if approval is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "not_found"})
        run = await runtime.database.get_run(approval.run_id)
        if run is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "not_found"})
        skill = await runtime.database.get_skill_by_id(run.skill_id)
        if skill is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"code": "not_found"})
        await runtime.authorization.require_skill(principal, skill, "approve")
        result = await runtime.dispatcher.decide_approval(
            approval_id,
            approve=payload.approve,
            decided_by_principal_id=principal.id,
            comment=payload.comment,
        )
        if result.get("status") == "invalid_approval":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "invalid_approval"},
            )
        await runtime.database.audit(
            "approval.decided",
            principal_id=principal.id,
            entity_type="approval",
            entity_id=approval.id,
            data={"approve": payload.approve, "run_id": approval.run_id},
        )
        return {
            key: value
            for key, value in result.items()
            if key in {"status", "approval_id", "run_id"}
        }

    instrument_fastapi(api, observability)
    return api


app = create_app()


def main() -> None:
    settings = Settings()
    uvicorn.run(
        create_app(settings),
        host=settings.api_host,
        port=settings.api_port,
        access_log=True,
    )
