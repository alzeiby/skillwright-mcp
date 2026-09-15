from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import suppress
from copy import deepcopy
from time import perf_counter
from typing import Any
from uuid import uuid4

from .auth import AuthorizationError, AuthorizationService
from .browser import BrowserActionResult, BrowserController
from .db import Database, RepairRow, RunRow, SkillRow, WorkflowVersionRow
from .secrets import (
    Redactor,
    SecretResolutionError,
    SecretResolver,
    secret_binding_from_marker,
    secret_marker,
)
from .snapshot import PageSnapshot, SnapshotElement, parse_snapshot
from .telemetry import (
    queue_wait_finished,
    run_finished,
    run_started,
    tracer,
    workflow_step_finished,
)
from .workflow import (
    AssertStep,
    ClickStep,
    ElementTarget,
    ExtractStep,
    FillStep,
    NavigateStep,
    SelectStep,
    WaitStep,
    WorkflowDefinition,
    WorkflowStep,
    render_template,
)

RUN_HEARTBEAT_INTERVAL_SECONDS = 5.0


class WorkflowEngine:
    def __init__(
        self,
        database: Database,
        browser: BrowserController,
        authorization: AuthorizationService | None = None,
        secret_resolver: SecretResolver | None = None,
    ) -> None:
        self.database = database
        self.browser = browser
        self.authorization = authorization
        self.secret_resolver = secret_resolver or SecretResolver()

    async def run_skill(
        self,
        name: str,
        *,
        inputs: dict[str, Any] | None = None,
        version: int | None = None,
        idempotency_key: str | None = None,
        requested_by_principal_id: str | None = None,
    ) -> dict[str, Any]:
        prepared = await self.prepare_run(
            name,
            inputs=inputs,
            version=version,
            idempotency_key=idempotency_key,
            requested_by_principal_id=requested_by_principal_id,
        )
        if prepared["status"] != "queued":
            return prepared
        return await self.execute_persisted_run(
            prepared["run_id"],
            worker_id=f"inline-{uuid4()}",
        )

    async def prepare_run(
        self,
        name: str,
        *,
        inputs: dict[str, Any] | None = None,
        version: int | None = None,
        idempotency_key: str | None = None,
        requested_by_principal_id: str | None = None,
    ) -> dict[str, Any]:
        stored = await self.database.get_workflow_version(name, version)
        if stored is None:
            return {"status": "not_found", "workflow": name, "version": version}
        skill, version_row = stored
        workflow = WorkflowDefinition.model_validate(version_row.definition)
        try:
            prepared_inputs = workflow.prepare_inputs(inputs)
        except ValueError as exc:
            return {"status": "invalid_inputs", "workflow": name, "error": str(exc)}

        secret_bindings = {
            binding.input_name: binding
            for binding in await self.database.skill_secret_bindings(skill.id)
        }
        for input_name, input_spec in workflow.inputs.items():
            if not input_spec.secret:
                continue
            binding = secret_bindings.get(input_name)
            if binding is None:
                return {
                    "status": "secret_unavailable",
                    "workflow": name,
                    "input": input_name,
                    "reason": "secret_binding_missing",
                }
            try:
                prepared_inputs[input_name] = secret_marker(
                    binding.secret_ref,
                    provider=binding.provider,
                )
            except ValueError:
                return {
                    "status": "secret_unavailable",
                    "workflow": name,
                    "input": input_name,
                    "reason": "secret_binding_invalid",
                }

        try:
            run = await self.database.create_run(
                skill=skill,
                version=version_row,
                inputs=prepared_inputs,
                status="queued",
                idempotency_key=idempotency_key,
                requested_by_principal_id=requested_by_principal_id,
            )
        except ValueError as exc:
            if "idempotency key" not in str(exc):
                raise
            return {
                "status": "idempotency_conflict",
                "workflow": name,
            }
        if run.status == "queued":
            await self.database.audit(
                "skill.run.queued",
                principal_id=requested_by_principal_id,
                entity_type="run",
                entity_id=run.id,
                data={"skill": name, "version": version_row.version},
            )
        return {
            "status": run.status,
            "run_id": run.id,
            "workflow": name,
            "workflow_version": run.workflow_version,
            "current_step": run.current_step,
            "outputs": run.outputs,
            "failure_context": run.failure_context,
        }

    async def execute_persisted_run(self, run_id: str, *, worker_id: str) -> dict[str, Any]:
        claimed = await self.database.claim_run(run_id, worker_id)
        if claimed is None:
            existing = await self.database.get_run(run_id)
            if existing is None:
                return {"status": "not_found", "run_id": run_id}
            return {
                "status": existing.status,
                "run_id": existing.id,
                "workflow_version": existing.workflow_version,
                "current_step": existing.current_step,
                "outputs": existing.outputs,
                "failure_context": existing.failure_context,
            }

        if claimed.started_at is not None:
            queue_wait_finished(
                duration_ms=max(
                    0.0,
                    (claimed.started_at - claimed.queued_at).total_seconds() * 1000,
                )
            )

        version_row = await self.database.get_workflow_version_by_id(claimed.workflow_version_id)
        skill = await self.database.get_skill_by_id(claimed.skill_id)
        if version_row is None or skill is None:
            updated = await self.database.update_owned_run(
                claimed.id,
                worker_id,
                status="failed",
                failure_context={"reason": "workflow_version_missing"},
                finish=True,
            )
            if not updated:
                return {
                    "status": "ignored",
                    "run_id": claimed.id,
                    "reason": "run_ownership_lost",
                }
            return {"status": "failed", "run_id": claimed.id, "error": "workflow version missing"}

        if claimed.requested_by_principal_id is not None and self.authorization is not None:
            try:
                principal = await self.authorization.principal_by_id(
                    claimed.requested_by_principal_id
                )
                await self.authorization.require_skill(principal, skill, "run")
            except AuthorizationError as exc:
                failure = {
                    "status": "failed",
                    "run_id": claimed.id,
                    "reason": "permission_revoked_before_execution",
                    "error": str(exc),
                    "side_effect_state": "not_started",
                }
                updated = await self.database.update_owned_run(
                    claimed.id,
                    worker_id,
                    status="failed",
                    failure_context=failure,
                    finish=True,
                )
                if not updated:
                    return {
                        "status": "ignored",
                        "run_id": claimed.id,
                        "reason": "run_ownership_lost",
                    }
                await self.database.audit(
                    "skill.run.permission_denied",
                    principal_id=claimed.requested_by_principal_id,
                    entity_type="run",
                    entity_id=claimed.id,
                    data={"skill": skill.name, "reason": str(exc)},
                )
                return failure

        definition = deepcopy(version_row.definition)
        for step_key, target in claimed.repair_overrides.items():
            index = int(step_key)
            if index < len(definition["steps"]) and "target" in definition["steps"][index]:
                definition["steps"][index]["target"] = target
        workflow = WorkflowDefinition.model_validate(definition)
        try:
            variables, redactor = await self._resolve_run_variables(claimed)
        except SecretResolutionError:
            failure = {
                "status": "failed",
                "run_id": claimed.id,
                "reason": "secret_unavailable",
                "side_effect_state": "not_started",
            }
            updated = await self.database.update_owned_run(
                claimed.id,
                worker_id,
                status="failed",
                failure_context=failure,
                finish=True,
            )
            if not updated:
                return {
                    "status": "ignored",
                    "run_id": claimed.id,
                    "reason": "run_ownership_lost",
                }
            await self.database.audit(
                "skill.run.secret_unavailable",
                principal_id=claimed.requested_by_principal_id,
                entity_type="run",
                entity_id=claimed.id,
                data={"skill": skill.name},
            )
            return failure
        await self.database.audit(
            "skill.run.started",
            principal_id=claimed.requested_by_principal_id,
            entity_type="run",
            entity_id=claimed.id,
            data={
                "skill": workflow.name,
                "version": version_row.version,
                "worker_id": worker_id,
                "attempt": claimed.attempt_count,
            },
        )
        return await self._run_execution_segment(
            workflow=workflow,
            skill=skill,
            version_row=version_row,
            run=claimed,
            start_index=claimed.current_step,
            variables=variables,
            attempt=max(1, claimed.attempt_count),
            worker_id=worker_id,
            redactor=redactor,
        )

    async def request_repair(
        self,
        run_id: str,
        *,
        step: int,
        replacement_element_id: str,
        persist: bool = True,
        actor_principal_id: str | None = None,
    ) -> dict[str, Any]:
        run = await self.database.get_run(run_id)
        if run is None:
            return {"status": "not_found", "run_id": run_id}
        if run.status != "repair_required" or not run.failure_context:
            return {
                "status": "not_repairable",
                "run_id": run_id,
                "run_status": run.status,
            }
        failure = run.failure_context
        if failure.get("step") != step:
            return {
                "status": "invalid_repair",
                "run_id": run_id,
                "error": f"run is waiting for repair at step {failure.get('step')}",
            }
        candidates = {candidate["id"]: candidate for candidate in failure.get("candidates", [])}
        candidate = candidates.get(replacement_element_id)
        if candidate is None:
            return {
                "status": "invalid_repair",
                "run_id": run_id,
                "error": f"unknown replacement element id: {replacement_element_id}",
            }
        version_row = await self.database.get_workflow_version_by_id(run.workflow_version_id)
        if version_row is None:
            return {"status": "invalid_run", "run_id": run_id, "error": "workflow version missing"}
        replacement_target = ElementTarget.model_validate(candidate["target"])
        try:
            repair, created = await self.database.create_repair(
                run_id=run.id,
                workflow_version_id=version_row.id,
                step_index=step,
                expected_target=failure.get("expected", {}),
                replacement_target=replacement_target.model_dump(mode="json"),
                candidate_id=replacement_element_id,
                persist_version=persist,
                status="pending",
                requested_by_principal_id=actor_principal_id or run.requested_by_principal_id,
            )
        except ValueError:
            latest = await self.database.get_run(run.id)
            if latest is None:
                return {"status": "not_found", "run_id": run.id}
            return {
                "status": latest.status if latest.status != "repair_required" else "not_repairable",
                "run_id": run.id,
            }
        if not created:
            if repair.step_index == step and repair.candidate_id == replacement_element_id:
                return {
                    "status": "repair_pending",
                    "run_id": run_id,
                    "repair_id": repair.id,
                    "step": step,
                    "replacement_element_id": replacement_element_id,
                }
            return {
                "status": "repair_conflict",
                "run_id": run_id,
                "repair_id": repair.id,
                "error": "another repair proposal is already pending for this run",
            }
        await self.database.audit(
            "repair.requested",
            principal_id=actor_principal_id or run.requested_by_principal_id,
            entity_type="repair",
            entity_id=repair.id,
            data={"run_id": run.id, "step": step, "candidate_id": replacement_element_id},
        )
        return {
            "status": "repair_pending",
            "run_id": run_id,
            "repair_id": repair.id,
            "step": step,
            "replacement_element_id": replacement_element_id,
        }

    async def apply_repair(
        self,
        repair: RepairRow,
        *,
        worker_id: str | None = None,
    ) -> dict[str, Any]:
        run = await self.database.get_run(repair.run_id)
        if run is None:
            return {"status": "not_found", "repair_id": repair.id, "run_id": repair.run_id}
        if run.status != "repair_required" or not run.failure_context:
            result = {
                "status": "not_repairable",
                "run_id": run.id,
                "run_status": run.status,
            }
            await self.database.complete_repair(
                repair.id,
                status="not_repairable",
                validation_result=result,
            )
            return result
        if worker_id is not None and run.worker_id != worker_id:
            result = {
                "status": "repair_session_mismatch",
                "run_id": run.id,
                "repair_id": repair.id,
            }
            await self.database.complete_repair(
                repair.id,
                status="session_mismatch",
                validation_result=result,
            )
            return result

        try:
            variables, redactor = await self._resolve_run_variables(run)
        except SecretResolutionError:
            result = {
                "status": "failed",
                "run_id": run.id,
                "reason": "secret_unavailable",
                "side_effect_state": "not_started",
            }
            if worker_id is None:
                await self.database.update_run(
                    run.id,
                    status="failed",
                    failure_context=result,
                    finish=True,
                )
            elif not await self.database.update_owned_run(
                run.id,
                worker_id,
                expected_status="repair_required",
                require_not_cancelled=True,
                status="failed",
                failure_context=result,
                finish=True,
            ):
                latest = await self.database.get_run(run.id)
                return {
                    "status": latest.status if latest is not None else "not_found",
                    "run_id": run.id,
                }
            await self.database.complete_repair(
                repair.id,
                status="run_incomplete",
                validation_result=result,
            )
            return result
        failure = run.failure_context
        if failure.get("step") != repair.step_index:
            result = {
                "status": "invalid_repair",
                "run_id": run.id,
                "error": f"run is waiting for repair at step {failure.get('step')}",
            }
            await self.database.complete_repair(
                repair.id,
                status="invalid",
                validation_result=result,
            )
            return result

        replacement_target = ElementTarget.model_validate(repair.replacement_target)
        validation_snapshot_result = await self.browser.snapshot(
            source="repair",
            run_id=run.id,
            actor_principal_id=repair.requested_by_principal_id or run.requested_by_principal_id,
            redactor=redactor,
        )
        if not validation_snapshot_result.ok or validation_snapshot_result.result is None:
            result = {
                "status": "repair_validation_failed",
                "run_id": run.id,
                "error": validation_snapshot_result.error or "could not capture repair snapshot",
            }
            await self.database.complete_repair(
                repair.id,
                status="validation_failed",
                validation_result=result,
            )
            return result
        validation_snapshot = parse_snapshot(validation_snapshot_result.result.text)
        current_element = validation_snapshot.resolve(replacement_target)
        if current_element is None:
            result = {
                "status": "repair_validation_failed",
                "run_id": run.id,
                "error": "replacement element is no longer uniquely present on the page",
            }
            await self.database.complete_repair(
                repair.id,
                status="validation_failed",
                validation_result=result,
            )
            return result
        generated_locator = await self.browser.generate_locator(
            current_element.ref,
            element=current_element.name,
            redactor=redactor,
        )
        if generated_locator:
            replacement_target = replacement_target.model_copy(
                update={"locator": generated_locator}
            )

        # Cancellation can race the live validation call above. request_cancel() marks the
        # run and repair terminal before returning; re-check here so this worker never reopens
        # a cancelled run merely to discover the cancel flag at the next step boundary.
        latest_run = await self.database.get_run(run.id)
        if latest_run is not None and (
            latest_run.cancel_requested or latest_run.status == "cancelled"
        ):
            result = {
                "status": "cancelled",
                "run_id": run.id,
                "repair_id": repair.id,
                "side_effect_state": "not_started",
            }
            await self.database.complete_repair(
                repair.id,
                status="cancelled",
                validation_result=result,
            )
            return result

        version_row = await self.database.get_workflow_version_by_id(run.workflow_version_id)
        skill = await self.database.get_skill_by_id(run.skill_id)
        if version_row is None or skill is None:
            result = {
                "status": "invalid_run",
                "run_id": run.id,
                "error": "workflow version missing",
            }
            await self.database.complete_repair(
                repair.id,
                status="invalid",
                validation_result=result,
            )
            return result
        workflow = WorkflowDefinition.model_validate(version_row.definition)
        definition = workflow.model_dump(mode="json")
        overrides = deepcopy(run.repair_overrides)
        replacement_target_json = replacement_target.model_dump(mode="json")
        overrides[str(repair.step_index)] = replacement_target_json
        for step_key, target in overrides.items():
            index = int(step_key)
            if index < len(definition["steps"]) and "target" in definition["steps"][index]:
                definition["steps"][index]["target"] = target
        patched_workflow = WorkflowDefinition.model_validate(definition)

        if worker_id is None:
            await self.database.update_run(
                run.id,
                status="running",
                repair_overrides=overrides,
                failure_context=None,
            )
        elif not await self.database.update_owned_run(
            run.id,
            worker_id,
            expected_status="repair_required",
            require_not_cancelled=True,
            status="running",
            repair_overrides=overrides,
            failure_context=None,
            heartbeat=True,
        ):
            latest = await self.database.get_run(run.id)
            result = {
                "status": latest.status if latest is not None else "not_found",
                "run_id": run.id,
                "repair_id": repair.id,
            }
            await self.database.complete_repair(
                repair.id,
                status="run_incomplete",
                validation_result=result,
            )
            return result
        run.repair_overrides = overrides
        run_result = await self._run_execution_segment(
            workflow=patched_workflow,
            skill=skill,
            version_row=version_row,
            run=run,
            start_index=repair.step_index,
            variables=variables,
            attempt=max(2, run.attempt_count + 1),
            worker_id=worker_id,
            redactor=redactor,
        )

        new_version_id: str | None = None
        if run_result["status"] == "succeeded" and repair.persist_version:
            try:
                _, new_version = await self.database.create_skill_version(
                    patched_workflow,
                    actor_principal_id=(
                        repair.requested_by_principal_id or run.requested_by_principal_id
                    ),
                    parent_version=run.workflow_version,
                    change_reason=f"repair run {run.id} step {repair.step_index}",
                    expected_current_version=run.workflow_version,
                )
                new_version_id = new_version.id
                run_result["saved_workflow_version"] = new_version.version
            except (ValueError, PermissionError) as exc:
                run_result["repair_persisted"] = False
                run_result["repair_persist_reason"] = str(exc)

        repair_status = (
            "succeeded"
            if run_result["status"] == "succeeded"
            else "cancelled"
            if run_result["status"] == "cancelled"
            else "applied" if run_result["status"] == "repair_required" else "run_incomplete"
        )
        await self.database.complete_repair(
            repair.id,
            status=repair_status,
            validation_result=run_result,
            new_workflow_version_id=new_version_id,
        )
        await self.database.audit(
            "repair.applied",
            principal_id=repair.requested_by_principal_id or run.requested_by_principal_id,
            entity_type="repair",
            entity_id=repair.id,
            data={"run_id": run.id, "status": repair_status},
        )
        return run_result

    async def repair(
        self,
        run_id: str,
        *,
        step: int,
        replacement_element_id: str,
        persist: bool = True,
        actor_principal_id: str | None = None,
    ) -> dict[str, Any]:
        requested = await self.request_repair(
            run_id,
            step=step,
            replacement_element_id=replacement_element_id,
            persist=persist,
            actor_principal_id=actor_principal_id,
        )
        if requested.get("status") != "repair_pending":
            return requested
        repair = await self.database.claim_pending_repair(run_id)
        if repair is None:
            return {
                "status": "repair_conflict",
                "run_id": run_id,
                "error": "repair proposal was claimed by another executor",
            }
        return await self.apply_repair(repair)

    async def resume_waiting_run(
        self,
        run_id: str,
        *,
        worker_id: str | None = None,
    ) -> dict[str, Any]:
        run = await self.database.get_run(run_id)
        if run is None:
            return {"status": "not_found", "run_id": run_id}
        if run.status != "approval_required":
            return {"status": run.status, "run_id": run.id}
        if worker_id is not None and run.worker_id != worker_id:
            return {
                "status": "ignored",
                "run_id": run.id,
                "reason": "approval_session_owned_by_other_worker",
            }

        version_row = await self.database.get_workflow_version_by_id(run.workflow_version_id)
        skill = await self.database.get_skill_by_id(run.skill_id)
        if version_row is None or skill is None:
            return {"status": "failed", "run_id": run.id, "error": "workflow version missing"}
        if run.requested_by_principal_id is not None and self.authorization is not None:
            try:
                principal = await self.authorization.principal_by_id(run.requested_by_principal_id)
                await self.authorization.require_skill(principal, skill, "run")
            except AuthorizationError as exc:
                failure = {
                    "status": "failed",
                    "run_id": run.id,
                    "reason": "permission_revoked_before_approved_action",
                    "error": str(exc),
                    "side_effect_state": "not_started",
                }
                if worker_id is None:
                    await self.database.update_run(
                        run.id,
                        status="failed",
                        failure_context=failure,
                        finish=True,
                    )
                elif not await self.database.update_owned_run(
                    run.id,
                    worker_id,
                    expected_status="approval_required",
                    require_not_cancelled=True,
                    status="failed",
                    failure_context=failure,
                    finish=True,
                ):
                    latest = await self.database.get_run(run.id)
                    return {
                        "status": latest.status if latest is not None else "not_found",
                        "run_id": run.id,
                    }
                return failure

        definition = deepcopy(version_row.definition)
        for step_key, target in run.repair_overrides.items():
            index = int(step_key)
            if index < len(definition["steps"]) and "target" in definition["steps"][index]:
                definition["steps"][index]["target"] = target
        workflow = WorkflowDefinition.model_validate(definition)
        try:
            variables, redactor = await self._resolve_run_variables(run)
        except SecretResolutionError:
            failure = {
                "status": "failed",
                "run_id": run.id,
                "reason": "secret_unavailable",
                "side_effect_state": "not_started",
            }
            if worker_id is None:
                await self.database.update_run(
                    run.id,
                    status="failed",
                    failure_context=failure,
                    finish=True,
                )
            elif not await self.database.update_owned_run(
                run.id,
                worker_id,
                expected_status="approval_required",
                require_not_cancelled=True,
                status="failed",
                failure_context=failure,
                finish=True,
            ):
                latest = await self.database.get_run(run.id)
                return {
                    "status": latest.status if latest is not None else "not_found",
                    "run_id": run.id,
                }
            return failure
        if worker_id is None:
            await self.database.update_run(
                run.id,
                status="running",
                failure_context=None,
            )
        elif not await self.database.update_owned_run(
            run.id,
            worker_id,
            expected_status="approval_required",
            require_not_cancelled=True,
            status="running",
            failure_context=None,
            heartbeat=True,
        ):
            latest = await self.database.get_run(run.id)
            return {
                "status": latest.status if latest is not None else "not_found",
                "run_id": run.id,
            }
        return await self._run_execution_segment(
            workflow=workflow,
            skill=skill,
            version_row=version_row,
            run=run,
            start_index=run.current_step,
            variables=variables,
            attempt=max(1, run.attempt_count),
            worker_id=worker_id,
            redactor=redactor,
        )

    async def _resolve_run_variables(self, run: RunRow) -> tuple[dict[str, Any], Redactor]:
        variables: dict[str, Any] = {}
        secret_bindings: list[tuple[str, str, str]] = []
        for name, stored_value in run.inputs.items():
            binding = secret_binding_from_marker(stored_value)
            if binding is None:
                variables[name] = stored_value
                continue
            provider, reference = binding
            secret_bindings.append((name, provider, reference))

        secret_values: list[str] = []
        if secret_bindings:
            resolved = await asyncio.gather(
                *(
                    self.secret_resolver.resolve(reference, provider=provider)
                    for _name, provider, reference in secret_bindings
                )
            )
            for (name, _provider, _reference), value in zip(
                secret_bindings,
                resolved,
                strict=True,
            ):
                variables[name] = value
                secret_values.append(value)

        variables.update(run.outputs)
        return variables, Redactor.from_values(secret_values)

    async def _run_execution_segment(
        self,
        *,
        workflow: WorkflowDefinition,
        skill: SkillRow,
        version_row: WorkflowVersionRow,
        run: RunRow,
        start_index: int,
        variables: dict[str, Any],
        attempt: int,
        worker_id: str | None = None,
        redactor: Redactor | None = None,
    ) -> dict[str, Any]:
        started = perf_counter()
        status = "failed"
        run_started()
        with tracer().start_as_current_span(
            "skill.run",
            attributes={
                "skillwright.run.id": run.id,
                "skillwright.workflow.version": version_row.version,
                "skillwright.run.attempt": attempt,
            },
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            heartbeat: asyncio.Task[None] | None = None
            ownership_lost = asyncio.Event()
            heartbeat_failure: list[BaseException] = []
            owner_task = asyncio.current_task()
            if worker_id is not None:
                if owner_task is None:
                    raise RuntimeError("workflow execution has no owning asyncio task")
                heartbeat = asyncio.create_task(
                    self._heartbeat_owned_run(
                        run.id,
                        worker_id,
                        owner_task=owner_task,
                        ownership_lost=ownership_lost,
                        heartbeat_failure=heartbeat_failure,
                    ),
                    name=f"skillwright-run-heartbeat-{run.id}",
                )
            try:
                try:
                    result = await self._execute(
                        workflow=workflow,
                        skill=skill,
                        version_row=version_row,
                        run=run,
                        start_index=start_index,
                        variables=variables,
                        attempt=attempt,
                        worker_id=worker_id,
                        redactor=redactor,
                    )
                except asyncio.CancelledError:
                    if ownership_lost.is_set() or heartbeat_failure:
                        current = asyncio.current_task()
                        if current is not None:
                            current.uncancel()
                        if heartbeat_failure:
                            raise RuntimeError("run heartbeat failed") from heartbeat_failure[0]
                        result = {
                            "status": "ignored",
                            "run_id": run.id,
                            "reason": "run_ownership_lost",
                        }
                    else:
                        raise
                status = str(result.get("status", "unknown"))
                span.set_attribute("skillwright.run.status", status)
                return result
            finally:
                if heartbeat is not None:
                    heartbeat.cancel()
                    with suppress(asyncio.CancelledError):
                        await heartbeat
                run_finished(
                    status=status,
                    duration_ms=(perf_counter() - started) * 1000,
                )

    async def _heartbeat_owned_run(
        self,
        run_id: str,
        worker_id: str,
        *,
        owner_task: asyncio.Task[Any],
        ownership_lost: asyncio.Event,
        heartbeat_failure: list[BaseException],
    ) -> None:
        try:
            while True:
                await asyncio.sleep(RUN_HEARTBEAT_INTERVAL_SECONDS)
                if await self.database.heartbeat_run(run_id, worker_id):
                    continue
                run = await self.database.get_run(run_id)
                if run is not None and run.worker_id == worker_id and run.status != "running":
                    return
                ownership_lost.set()
                owner_task.cancel()
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            heartbeat_failure.append(exc)
            owner_task.cancel()

    async def _execution_interrupted(
        self,
        *,
        run: RunRow,
        workflow: WorkflowDefinition,
        version_row: WorkflowVersionRow,
        index: int,
        outputs: dict[str, Any],
        worker_id: str | None,
    ) -> dict[str, Any]:
        if worker_id is None:
            await self.database.update_run(
                run.id,
                status="cancelled",
                current_step=index,
                outputs=outputs,
                failure_context=None,
                finish=True,
            )
            return {
                "status": "cancelled",
                "run_id": run.id,
                "workflow": workflow.name,
                "workflow_version": version_row.version,
                "steps_completed": index,
                "outputs": outputs,
            }

        latest = await self.database.get_run(run.id)
        if (
            latest is not None
            and latest.status == "running"
            and latest.worker_id == worker_id
            and latest.cancel_requested
            and await self.database.update_owned_run(
                run.id,
                worker_id,
                status="cancelled",
                current_step=index,
                outputs=outputs,
                failure_context=None,
                finish=True,
            )
        ):
            return {
                "status": "cancelled",
                "run_id": run.id,
                "workflow": workflow.name,
                "workflow_version": version_row.version,
                "steps_completed": index,
                "outputs": outputs,
            }
        return {
            "status": "ignored",
            "run_id": run.id,
            "reason": "run_ownership_lost",
        }

    async def _execute(
        self,
        *,
        workflow: WorkflowDefinition,
        skill: SkillRow,
        version_row: WorkflowVersionRow,
        run: RunRow,
        start_index: int,
        variables: dict[str, Any],
        attempt: int,
        worker_id: str | None = None,
        redactor: Redactor | None = None,
    ) -> dict[str, Any]:
        outputs = dict(run.outputs)
        for index in range(start_index, len(workflow.steps)):
            if await self.database.is_cancel_requested(run.id):
                return await self._execution_interrupted(
                    run=run,
                    workflow=workflow,
                    version_row=version_row,
                    index=index,
                    outputs=outputs,
                    worker_id=worker_id,
                )
            if worker_id is not None and not await self.database.heartbeat_run(run.id, worker_id):
                return {
                    "status": "ignored",
                    "run_id": run.id,
                    "reason": "run_ownership_lost",
                }
            step = workflow.steps[index]
            if worker_id is None:
                await self.database.update_run(
                    run.id,
                    current_step=index,
                    outputs=outputs,
                )
            elif not await self.database.update_owned_run(
                run.id,
                worker_id,
                current_step=index,
                outputs=outputs,
                heartbeat=True,
            ):
                return await self._execution_interrupted(
                    run=run,
                    workflow=workflow,
                    version_row=version_row,
                    index=index,
                    outputs=outputs,
                    worker_id=worker_id,
                )
            approval_gate = getattr(step, "approval", None)
            if approval_gate is not None:
                gate_fingerprint = _approval_fingerprint(step)
                approval = await self.database.approval_for_gate(
                    run_id=run.id,
                    workflow_version_id=version_row.id,
                    step_index=index,
                    gate_fingerprint=gate_fingerprint,
                )
                if approval is None or approval.status != "approved":
                    if approval is not None and approval.status == "rejected":
                        return {
                            "status": "rejected",
                            "run_id": run.id,
                            "approval_id": approval.id,
                            "step": index,
                            "side_effect_state": "not_started",
                        }
                    approval = await self.database.get_or_create_approval(
                        run_id=run.id,
                        workflow_version_id=version_row.id,
                        step_index=index,
                        gate_fingerprint=gate_fingerprint,
                        reason=approval_gate.reason,
                        requested_by_principal_id=run.requested_by_principal_id,
                    )
                    gated_target = getattr(step, "target", None)
                    context = {
                        "status": "approval_required",
                        "run_id": run.id,
                        "workflow": workflow.name,
                        "workflow_version": version_row.version,
                        "step": index,
                        "operation": step.op,
                        "approval_id": approval.id,
                        "reason": approval.reason,
                        "target": (
                            gated_target.model_dump(mode="json")
                            if isinstance(gated_target, ElementTarget)
                            else None
                        ),
                        "side_effect_state": "not_started",
                        "session_available": True,
                    }
                    recorded = await self.database.add_step_execution(
                        run_id=run.id,
                        step_index=index,
                        attempt=attempt,
                        status="approval_required",
                        step=step.model_dump(mode="json"),
                        resolved_target=None,
                        result={"approval_id": approval.id},
                        error=None,
                        duration_ms=0.0,
                        worker_id=worker_id,
                    )
                    if worker_id is not None and recorded is None:
                        return await self._execution_interrupted(
                            run=run,
                            workflow=workflow,
                            version_row=version_row,
                            index=index,
                            outputs=outputs,
                            worker_id=worker_id,
                        )
                    if worker_id is None:
                        await self.database.update_run(
                            run.id,
                            status="approval_required",
                            current_step=index,
                            outputs=outputs,
                            failure_context=context,
                        )
                    elif not await self.database.update_owned_run(
                        run.id,
                        worker_id,
                        status="approval_required",
                        current_step=index,
                        outputs=outputs,
                        failure_context=context,
                        heartbeat=True,
                    ):
                        return await self._execution_interrupted(
                            run=run,
                            workflow=workflow,
                            version_row=version_row,
                            index=index,
                            outputs=outputs,
                            worker_id=worker_id,
                        )
                    if await self.database.is_cancel_requested(run.id):
                        await self.database.request_cancel(run.id)
                        return {
                            "status": "cancelled",
                            "run_id": run.id,
                            "workflow": workflow.name,
                            "workflow_version": version_row.version,
                            "steps_completed": index,
                            "outputs": outputs,
                        }
                    await self.database.audit(
                        "approval.requested",
                        principal_id=run.requested_by_principal_id,
                        entity_type="approval",
                        entity_id=approval.id,
                        data={"run_id": run.id, "step": index, "operation": step.op},
                    )
                    return context
            with tracer().start_as_current_span(
                "workflow.step",
                attributes={
                    "skillwright.run.id": run.id,
                    "skillwright.workflow.step.index": index,
                    "skillwright.workflow.step.operation": step.op,
                },
                record_exception=False,
                set_status_on_exception=False,
            ) as span:
                started = perf_counter()
                execution = await self._execute_step(
                    step,
                    variables,
                    run.id,
                    actor_principal_id=run.requested_by_principal_id,
                    redactor=redactor,
                )
                duration_ms = (perf_counter() - started) * 1000
                execution_status = str(execution.get("status", "unknown"))
                span.set_attribute("skillwright.workflow.step.status", execution_status)
                workflow_step_finished(
                    operation=step.op,
                    status=execution_status,
                    duration_ms=duration_ms,
                )
            if worker_id is not None and not await self.database.heartbeat_run(run.id, worker_id):
                return await self._execution_interrupted(
                    run=run,
                    workflow=workflow,
                    version_row=version_row,
                    index=index,
                    outputs=outputs,
                    worker_id=worker_id,
                )
            if execution["status"] == "repair_required":
                failure = self._repair_context(
                    run=run,
                    workflow=workflow,
                    version_row=version_row,
                    step_index=index,
                    step=step,
                    snapshot=execution["snapshot"],
                    reason=execution["reason"],
                )
                recorded = await self.database.add_step_execution(
                    run_id=run.id,
                    step_index=index,
                    attempt=attempt,
                    status="repair_required",
                    step=step.model_dump(mode="json"),
                    resolved_target=None,
                    result=None,
                    error=execution["reason"],
                    duration_ms=duration_ms,
                    worker_id=worker_id,
                )
                if worker_id is not None and recorded is None:
                    return await self._execution_interrupted(
                        run=run,
                        workflow=workflow,
                        version_row=version_row,
                        index=index,
                        outputs=outputs,
                        worker_id=worker_id,
                    )
                if worker_id is None:
                    await self.database.update_run(
                        run.id,
                        status="repair_required",
                        current_step=index,
                        outputs=outputs,
                        failure_context=failure,
                    )
                elif not await self.database.update_owned_run(
                    run.id,
                    worker_id,
                    status="repair_required",
                    current_step=index,
                    outputs=outputs,
                    failure_context=failure,
                ):
                    return await self._execution_interrupted(
                        run=run,
                        workflow=workflow,
                        version_row=version_row,
                        index=index,
                        outputs=outputs,
                        worker_id=worker_id,
                    )
                if await self.database.is_cancel_requested(run.id):
                    await self.database.request_cancel(run.id)
                    return {
                        "status": "cancelled",
                        "run_id": run.id,
                        "workflow": workflow.name,
                        "workflow_version": version_row.version,
                        "steps_completed": index,
                        "outputs": outputs,
                    }
                return failure

            if execution["status"] == "failed":
                failure = {
                    "status": "failed",
                    "run_id": run.id,
                    "workflow": workflow.name,
                    "workflow_version": version_row.version,
                    "step": index,
                    "operation": step.op,
                    "error": execution["error"],
                    "side_effect_state": execution.get("side_effect_state", "not_started"),
                }
                recorded = await self.database.add_step_execution(
                    run_id=run.id,
                    step_index=index,
                    attempt=attempt,
                    status="failed",
                    step=step.model_dump(mode="json"),
                    resolved_target=execution.get("resolved_target"),
                    result=execution.get("result"),
                    error=execution["error"],
                    duration_ms=duration_ms,
                    worker_id=worker_id,
                )
                if worker_id is not None and recorded is None:
                    return await self._execution_interrupted(
                        run=run,
                        workflow=workflow,
                        version_row=version_row,
                        index=index,
                        outputs=outputs,
                        worker_id=worker_id,
                    )
                if worker_id is None:
                    await self.database.update_run(
                        run.id,
                        status="failed",
                        current_step=index,
                        outputs=outputs,
                        failure_context=failure,
                        finish=True,
                    )
                elif not await self.database.update_owned_run(
                    run.id,
                    worker_id,
                    require_not_cancelled=True,
                    status="failed",
                    current_step=index,
                    outputs=outputs,
                    failure_context=failure,
                    finish=True,
                ):
                    return await self._execution_interrupted(
                        run=run,
                        workflow=workflow,
                        version_row=version_row,
                        index=index,
                        outputs=outputs,
                        worker_id=worker_id,
                    )
                return failure

            if "output" in execution:
                output_name, output_value = execution["output"]
                outputs[output_name] = output_value
                variables[output_name] = output_value
            recorded = await self.database.add_step_execution(
                run_id=run.id,
                step_index=index,
                attempt=attempt,
                status="succeeded",
                step=step.model_dump(mode="json"),
                resolved_target=execution.get("resolved_target"),
                result=execution.get("result"),
                error=None,
                duration_ms=duration_ms,
                worker_id=worker_id,
            )
            if worker_id is not None and recorded is None:
                return await self._execution_interrupted(
                    run=run,
                    workflow=workflow,
                    version_row=version_row,
                    index=index,
                    outputs=outputs,
                    worker_id=worker_id,
                )

        if worker_id is None:
            await self.database.update_run(
                run.id,
                status="succeeded",
                current_step=len(workflow.steps),
                outputs=outputs,
                failure_context=None,
                finish=True,
            )
        elif not await self.database.update_owned_run(
            run.id,
            worker_id,
            require_not_cancelled=True,
            status="succeeded",
            current_step=len(workflow.steps),
            outputs=outputs,
            failure_context=None,
            finish=True,
        ):
            return await self._execution_interrupted(
                run=run,
                workflow=workflow,
                version_row=version_row,
                index=len(workflow.steps),
                outputs=outputs,
                worker_id=worker_id,
            )
        await self.database.audit(
            "skill.run.succeeded",
            principal_id=run.requested_by_principal_id,
            entity_type="run",
            entity_id=run.id,
            data={"skill": workflow.name, "version": version_row.version},
        )
        return {
            "status": "succeeded",
            "run_id": run.id,
            "workflow": workflow.name,
            "workflow_version": version_row.version,
            "steps_completed": len(workflow.steps),
            "outputs": outputs,
        }

    async def _execute_step(
        self,
        step: WorkflowStep,
        variables: dict[str, Any],
        run_id: str,
        *,
        actor_principal_id: str | None,
        redactor: Redactor | None,
    ) -> dict[str, Any]:
        if isinstance(step, NavigateStep):
            action = await self.browser.navigate(
                render_template(step.url, variables),
                source="replay",
                run_id=run_id,
                actor_principal_id=actor_principal_id,
                redactor=redactor,
            )
            return _action_outcome(action, mutating=False)
        if isinstance(step, WaitStep):
            action = await self.browser.wait(
                seconds=step.seconds,
                text=render_template(step.text, variables) if step.text else None,
                text_gone=render_template(step.text_gone, variables) if step.text_gone else None,
                source="replay",
                run_id=run_id,
                actor_principal_id=actor_principal_id,
                redactor=redactor,
            )
            return _action_outcome(action, mutating=False)

        snapshot_action = await self.browser.snapshot(
            source="replay",
            run_id=run_id,
            actor_principal_id=actor_principal_id,
            redactor=redactor,
        )
        if not snapshot_action.ok or snapshot_action.result is None:
            return {
                "status": "failed",
                "error": snapshot_action.error or "could not capture page snapshot",
                "side_effect_state": "not_started",
            }
        snapshot = parse_snapshot(snapshot_action.result.text)

        if isinstance(step, AssertStep):
            if step.target is not None:
                resolved = await self._resolve_target(
                    step.target,
                    snapshot,
                    run_id,
                    actor_principal_id=actor_principal_id,
                    redactor=redactor,
                )
                if resolved is None:
                    return {
                        "status": "repair_required",
                        "reason": "target_unresolved",
                        "snapshot": snapshot,
                    }
            return _execute_assert(
                step, snapshot, variables, resolved_target=resolved if step.target else None
            )
        if isinstance(step, ExtractStep):
            element = await self._resolve_target(
                step.target,
                snapshot,
                run_id,
                actor_principal_id=actor_principal_id,
                redactor=redactor,
            )
            if element is None:
                return {
                    "status": "repair_required",
                    "reason": "target_unresolved",
                    "snapshot": snapshot,
                }
            if step.attribute is None:
                value = element.name
            else:
                value = element.attributes.get(step.attribute)
                if value is None:
                    return {
                        "status": "failed",
                        "error": f"attribute {step.attribute!r} is not present on resolved element",
                        "side_effect_state": "not_started",
                        "resolved_target": _resolved_element(element),
                    }
            return {
                "status": "succeeded",
                "resolved_target": _resolved_element(element),
                "output": (step.save_as, value),
                "result": {"value": value},
            }

        target = step.target
        element = await self._resolve_target(
            target,
            snapshot,
            run_id,
            actor_principal_id=actor_principal_id,
            redactor=redactor,
        )
        if element is None:
            return {
                "status": "repair_required",
                "reason": "target_unresolved",
                "snapshot": snapshot,
            }

        if isinstance(step, ClickStep):
            action = await self.browser.click(
                element.ref,
                element=target.recorded_description or element.name,
                double_click=step.double_click,
                button=step.button,
                source="replay",
                run_id=run_id,
                actor_principal_id=actor_principal_id,
                redactor=redactor,
            )
            outcome = _action_outcome(action, mutating=True)
        elif isinstance(step, FillStep):
            action = await self.browser.fill(
                element.ref,
                render_template(step.value, variables),
                element=target.recorded_description or element.name,
                submit=step.submit,
                source="replay",
                run_id=run_id,
                actor_principal_id=actor_principal_id,
                redactor=redactor,
            )
            outcome = _action_outcome(action, mutating=True)
        elif isinstance(step, SelectStep):
            action = await self.browser.select(
                element.ref,
                [render_template(value, variables) for value in step.values],
                element=target.recorded_description or element.name,
                source="replay",
                run_id=run_id,
                actor_principal_id=actor_principal_id,
                redactor=redactor,
            )
            outcome = _action_outcome(action, mutating=True)
        else:  # pragma: no cover - discriminated union keeps this exhaustive
            raise TypeError(f"unsupported workflow step: {type(step).__name__}")
        outcome["resolved_target"] = _resolved_element(element)
        return outcome

    async def _resolve_target(
        self,
        target: ElementTarget,
        full_snapshot: PageSnapshot,
        run_id: str,
        *,
        actor_principal_id: str | None,
        redactor: Redactor | None,
    ) -> SnapshotElement | None:
        if target.locator:
            locator_snapshot = await self.browser.snapshot(
                target=target.locator,
                depth=2,
                source="replay",
                run_id=run_id,
                actor_principal_id=actor_principal_id,
                redactor=redactor,
            )
            if locator_snapshot.ok and locator_snapshot.result is not None:
                targeted = parse_snapshot(locator_snapshot.result.text)
                element = targeted.resolve(target)
                if element is not None:
                    return element
        return full_snapshot.resolve(target)

    def _repair_context(
        self,
        *,
        run: RunRow,
        workflow: WorkflowDefinition,
        version_row: WorkflowVersionRow,
        step_index: int,
        step: WorkflowStep,
        snapshot: PageSnapshot,
        reason: str,
    ) -> dict[str, Any]:
        target = getattr(step, "target", None)
        assert isinstance(target, ElementTarget)
        candidates = []
        for candidate_index, (element, score) in enumerate(snapshot.ranked_candidates(target)):
            candidates.append(
                {
                    "id": f"el_{candidate_index}",
                    "runtime_ref": element.ref,
                    "role": element.role,
                    "name": element.name,
                    "score": round(score, 4),
                    "target": element.to_target(
                        description=element.name,
                        page_url=snapshot.url,
                    ).model_dump(mode="json"),
                }
            )
        return {
            "status": "repair_required",
            "run_id": run.id,
            "workflow": workflow.name,
            "workflow_version": version_row.version,
            "step": step_index,
            "operation": step.op,
            "reason": reason,
            "expected": target.model_dump(mode="json"),
            "page": {"url": snapshot.url, "title": snapshot.title},
            "candidates": candidates,
            "session_available": True,
            "side_effect_state": "not_started",
        }


def _execute_assert(
    step: AssertStep,
    snapshot: PageSnapshot,
    variables: dict[str, Any],
    *,
    resolved_target: SnapshotElement | None,
) -> dict[str, Any]:
    checks = (
        (step.url_contains, snapshot.url, "URL"),
        (step.title_contains, snapshot.title, "title"),
        (step.text_contains, snapshot.raw, "page text"),
    )
    for expected, actual, label in checks:
        if expected is None:
            continue
        rendered = render_template(expected, variables)
        if rendered.casefold() not in (actual or "").casefold():
            return {
                "status": "failed",
                "error": f"assertion failed: {label} does not contain {rendered!r}",
                "side_effect_state": "not_started",
            }
    result: dict[str, Any] = {"status": "succeeded", "result": {"asserted": True}}
    if resolved_target is not None:
        result["resolved_target"] = _resolved_element(resolved_target)
    return result


def _action_outcome(action: BrowserActionResult, *, mutating: bool) -> dict[str, Any]:
    if action.ok:
        return {
            "status": "succeeded",
            "result": action.result.as_dict() if action.result is not None else None,
        }
    return {
        "status": "failed",
        "error": action.error or "browser action failed",
        "result": action.result.as_dict() if action.result is not None else None,
        "side_effect_state": "unknown" if mutating else "not_started",
    }


def _approval_fingerprint(step: WorkflowStep) -> str:
    payload = step.model_dump(mode="json")
    payload.pop("approval", None)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _resolved_element(element: SnapshotElement) -> dict[str, Any]:
    return {"runtime_ref": element.ref, "role": element.role, "name": element.name}
