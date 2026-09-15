from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .browser import BrowserController
from .compiler import WorkflowCompilationError, compile_actions
from .db import Database
from .engine import WorkflowEngine
from .secrets import SecretProvider, validate_secret_ref
from .workflow import ApprovalGate, ParameterBinding, WorkflowDefinition, WorkflowInput


class SkillService:
    def __init__(
        self, database: Database, browser: BrowserController, engine: WorkflowEngine
    ) -> None:
        self.database = database
        self.browser = browser
        self.engine = engine

    async def record_start(
        self,
        name: str,
        description: str = "",
        *,
        owner_principal_id: str | None = None,
    ) -> dict[str, Any]:
        active_recording_id = self.browser.active_recording_for(owner_principal_id)
        if active_recording_id is not None:
            return {
                "status": "already_recording",
                "recording_id": active_recording_id,
            }
        row = await self.database.start_recording(
            name=name,
            description=description,
            owner_principal_id=owner_principal_id,
        )
        self.browser.set_active_recording(owner_principal_id, row.id)
        await self.database.audit(
            "skill.recording.started",
            principal_id=owner_principal_id,
            entity_type="recording",
            entity_id=row.id,
            data={"name": name},
        )
        return {"status": "recording", "recording_id": row.id, "name": name}

    async def record_stop(self, *, owner_principal_id: str | None = None) -> dict[str, Any]:
        recording_id = self.browser.active_recording_for(owner_principal_id)
        if recording_id is None:
            return {"status": "not_recording"}
        self.browser.set_active_recording(owner_principal_id, None)
        recording = await self.database.get_recording(recording_id)
        if recording is None:
            return {"status": "error", "error": "active recording disappeared"}
        if (
            owner_principal_id is not None
            and recording.owner_principal_id != owner_principal_id
        ):
            return {"status": "forbidden", "error": "recording belongs to another principal"}
        actions = await self.database.recording_actions(recording_id)
        try:
            workflow = compile_actions(
                name=recording.name,
                description=recording.description,
                actions=actions,
            )
        except WorkflowCompilationError as exc:
            await self.database.stop_recording(recording_id, status="compile_failed")
            return {
                "status": "compile_failed",
                "recording_id": recording_id,
                "error": str(exc),
            }
        skill, version = await self.database.create_skill_version(
            workflow,
            owner_principal_id=owner_principal_id,
            actor_principal_id=owner_principal_id,
            recording_id=recording_id,
            change_reason="recording",
        )
        await self._bind_recorded_secrets(
            skill_id=skill.id,
            actions=actions,
            principal_id=owner_principal_id,
        )
        await self.database.stop_recording(recording_id, status="compiled")
        await self.database.audit(
            "skill.version.created",
            principal_id=owner_principal_id,
            entity_type="skill",
            entity_id=skill.id,
            data={"name": skill.name, "version": version.version, "recording_id": recording_id},
        )
        return {
            "status": "saved",
            "recording_id": recording_id,
            "skill": skill.name,
            "version": version.version,
            "steps": len(workflow.steps),
            "workflow": workflow.model_dump(mode="json"),
        }

    async def save_from_history(
        self,
        name: str,
        *,
        start_event: int,
        end_event: int,
        description: str = "",
        owner_principal_id: str | None = None,
    ) -> dict[str, Any]:
        actions = await self.database.action_range(
            start_event,
            end_event,
            actor_principal_id=owner_principal_id,
        )
        if not actions:
            return {"status": "not_found", "error": "no browser actions in requested range"}
        try:
            workflow = compile_actions(name=name, description=description, actions=actions)
        except WorkflowCompilationError as exc:
            return {"status": "compile_failed", "error": str(exc)}
        skill, version = await self.database.create_skill_version(
            workflow,
            owner_principal_id=owner_principal_id,
            actor_principal_id=owner_principal_id,
            change_reason=f"history events {start_event}-{end_event}",
        )
        await self._bind_recorded_secrets(
            skill_id=skill.id,
            actions=actions,
            principal_id=owner_principal_id,
        )
        return {
            "status": "saved",
            "skill": skill.name,
            "version": version.version,
            "steps": len(workflow.steps),
            "workflow": workflow.model_dump(mode="json"),
        }

    async def list(self) -> dict[str, Any]:
        rows = await self.database.list_skills()
        return {
            "skills": [
                {
                    "name": row.name,
                    "description": row.description,
                    "current_version": row.current_version,
                }
                for row in rows
            ]
        }

    async def search(self, query: str, limit: int = 10) -> dict[str, Any]:
        rows = await self.database.search_skills(query, limit)
        return {
            "query": query,
            "skills": [
                {
                    "name": row.name,
                    "description": row.description,
                    "current_version": row.current_version,
                }
                for row in rows
            ],
        }

    async def get(self, name: str, version: int | None = None) -> dict[str, Any]:
        stored = await self.database.get_workflow_version(name, version)
        if stored is None:
            return {"status": "not_found", "skill": name, "version": version}
        skill, version_row = stored
        return {
            "status": "found",
            "skill": skill.name,
            "version": version_row.version,
            "current_version": skill.current_version,
            "workflow": version_row.definition,
            "change_reason": version_row.change_reason,
            "parent_version": version_row.parent_version,
        }

    async def versions(self, name: str) -> dict[str, Any]:
        skill = await self.database.get_skill(name)
        if skill is None:
            return {"status": "not_found", "skill": name}
        versions = await self.database.workflow_versions(name)
        return {
            "status": "found",
            "skill": name,
            "current_version": skill.current_version,
            "versions": [
                {
                    "version": row.version,
                    "parent_version": row.parent_version,
                    "change_reason": row.change_reason,
                    "created_at": row.created_at.isoformat(),
                }
                for row in versions
            ],
        }

    async def rollback(
        self,
        name: str,
        version: int,
        *,
        actor_principal_id: str | None = None,
    ) -> dict[str, Any]:
        stored = await self.database.get_workflow_version(name, version)
        if stored is None:
            return {"status": "not_found", "skill": name, "version": version}
        skill, old_version = stored
        workflow = WorkflowDefinition.model_validate(old_version.definition)
        _, new_version = await self.database.create_skill_version(
            workflow,
            actor_principal_id=actor_principal_id,
            parent_version=skill.current_version,
            change_reason=f"rollback to v{version}",
        )
        return {
            "status": "saved",
            "skill": name,
            "rolled_back_to": version,
            "new_version": new_version.version,
        }

    async def status(self, run_id: str) -> dict[str, Any]:
        run = await self.database.get_run(run_id)
        if run is None:
            return {"status": "not_found", "run_id": run_id}
        return {
            "status": run.status,
            "run_id": run.id,
            "workflow_version": run.workflow_version,
            "current_step": run.current_step,
            "outputs": run.outputs,
            "failure_context": run.failure_context,
        }

    async def parameterize(
        self,
        name: str,
        bindings: Sequence[ParameterBinding],
        *,
        actor_principal_id: str | None = None,
    ) -> dict[str, Any]:
        stored = await self.database.get_workflow_version(name)
        if stored is None:
            return {"status": "not_found", "skill": name}
        skill, version = stored
        definition = WorkflowDefinition.model_validate(version.definition).model_dump(mode="json")

        for binding in bindings:
            if binding.step >= len(definition["steps"]):
                return {
                    "status": "invalid_binding",
                    "error": f"step {binding.step} does not exist",
                }
            step = definition["steps"][binding.step]
            error = _apply_parameter_binding(step, binding)
            if error:
                return {"status": "invalid_binding", "error": error}
            existing = definition["inputs"].get(binding.input_name)
            input_spec = WorkflowInput(
                type=binding.input_type,
                description=binding.description,
                secret=binding.secret,
            ).model_dump(mode="json")
            if existing is not None and existing != input_spec:
                return {
                    "status": "invalid_binding",
                    "error": f"input {binding.input_name!r} is already defined differently",
                }
            definition["inputs"][binding.input_name] = input_spec

        if any(binding.secret for binding in bindings):
            definition["schema_version"] = 3

        updated = WorkflowDefinition.model_validate(definition)
        try:
            _, new_version = await self.database.create_skill_version(
                updated,
                actor_principal_id=actor_principal_id,
                parent_version=version.version,
                change_reason="parameterized inputs",
                expected_current_version=version.version,
            )
        except ValueError as exc:
            return {"status": "conflict", "error": str(exc)}
        return {
            "status": "saved",
            "skill": skill.name,
            "version": new_version.version,
            "inputs": updated.model_dump(mode="json")["inputs"],
            "workflow": updated.model_dump(mode="json"),
        }

    async def bind_secret(
        self,
        name: str,
        *,
        input_name: str,
        secret_ref: str,
        provider: SecretProvider = "env",
        principal_id: str | None,
    ) -> dict[str, Any]:
        stored = await self.database.get_workflow_version(name)
        if stored is None:
            return {"status": "not_found", "skill": name}
        skill, version = stored
        workflow = WorkflowDefinition.model_validate(version.definition)
        input_spec = workflow.inputs.get(input_name)
        if input_spec is None or not input_spec.secret:
            return {
                "status": "invalid_secret_input",
                "skill": name,
                "input": input_name,
            }
        try:
            normalized_ref = validate_secret_ref(secret_ref, provider=provider)
        except ValueError as exc:
            return {"status": "invalid_secret_ref", "error": str(exc)}
        await self.database.bind_skill_secret(
            skill_id=skill.id,
            input_name=input_name,
            provider=provider,
            secret_ref=normalized_ref,
            updated_by_principal_id=principal_id,
        )
        await self.database.audit(
            "skill.secret.bound",
            principal_id=principal_id,
            entity_type="skill",
            entity_id=skill.id,
            data={"input": input_name, "provider": provider, "configured": True},
        )
        return {
            "status": "bound",
            "skill": name,
            "input": input_name,
            "provider": provider,
            "configured": True,
        }

    async def unbind_secret(
        self,
        name: str,
        *,
        input_name: str,
        principal_id: str | None,
    ) -> dict[str, Any]:
        skill = await self.database.get_skill(name)
        if skill is None:
            return {"status": "not_found", "skill": name}
        removed = await self.database.unbind_skill_secret(
            skill_id=skill.id,
            input_name=input_name,
        )
        if removed:
            await self.database.audit(
                "skill.secret.unbound",
                principal_id=principal_id,
                entity_type="skill",
                entity_id=skill.id,
                data={"input": input_name, "configured": False},
            )
        return {
            "status": "unbound" if removed else "not_bound",
            "skill": name,
            "input": input_name,
            "configured": False,
        }

    async def secret_status(self, name: str) -> dict[str, Any]:
        stored = await self.database.get_workflow_version(name)
        if stored is None:
            return {"status": "not_found", "skill": name}
        skill, version = stored
        workflow = WorkflowDefinition.model_validate(version.definition)
        bindings = {
            row.input_name: row for row in await self.database.skill_secret_bindings(skill.id)
        }
        return {
            "status": "found",
            "skill": name,
            "secrets": [
                {
                    "input": input_name,
                    "configured": input_name in bindings,
                    "provider": bindings[input_name].provider if input_name in bindings else None,
                }
                for input_name, spec in workflow.inputs.items()
                if spec.secret
            ],
        }

    async def set_approval_gate(
        self,
        name: str,
        *,
        step: int,
        required: bool,
        reason: str | None = None,
        actor_principal_id: str | None = None,
    ) -> dict[str, Any]:
        stored = await self.database.get_workflow_version(name)
        if stored is None:
            return {"status": "not_found", "skill": name}
        skill, version = stored
        workflow = WorkflowDefinition.model_validate(version.definition)
        if step >= len(workflow.steps):
            return {"status": "invalid_step", "error": f"step {step} does not exist"}
        selected = workflow.steps[step]
        if selected.op not in {"click", "fill", "select"}:
            return {
                "status": "invalid_step",
                "error": f"approval gates are not supported for {selected.op!r} steps",
            }
        if required and not reason:
            return {"status": "invalid_approval", "error": "a required gate needs a reason"}

        definition = workflow.model_dump(mode="json")
        definition["schema_version"] = max(int(definition["schema_version"]), 2)
        definition["steps"][step]["approval"] = (
            ApprovalGate(reason=reason or "Approval required").model_dump(mode="json")
            if required
            else None
        )
        updated = WorkflowDefinition.model_validate(definition)
        try:
            _, new_version = await self.database.create_skill_version(
                updated,
                actor_principal_id=actor_principal_id,
                parent_version=version.version,
                change_reason=(
                    f"require approval at step {step}"
                    if required
                    else f"remove approval at step {step}"
                ),
                expected_current_version=version.version,
            )
        except ValueError as exc:
            return {"status": "conflict", "error": str(exc)}
        return {
            "status": "saved",
            "skill": skill.name,
            "version": new_version.version,
            "step": step,
            "approval": definition["steps"][step]["approval"],
        }

    async def _bind_recorded_secrets(
        self,
        *,
        skill_id: str,
        actions: Sequence[Any],
        principal_id: str | None,
    ) -> None:
        seen: dict[str, tuple[str, str]] = {}
        for action in actions:
            if action.tool_name != "browser_fill_secret":
                continue
            input_name = action.arguments.get("input_name")
            secret_ref = action.arguments.get("secret_ref")
            provider = action.arguments.get("provider", "env")
            if (
                not isinstance(input_name, str)
                or not isinstance(secret_ref, str)
                or not isinstance(provider, str)
            ):
                continue
            previous = seen.get(input_name)
            binding = (provider, secret_ref)
            if previous is not None and previous != binding:
                raise WorkflowCompilationError(
                    f"recording uses multiple secret bindings for input {input_name!r}"
                )
            seen[input_name] = binding
        for input_name, (provider, secret_ref) in seen.items():
            await self.database.bind_skill_secret(
                skill_id=skill_id,
                input_name=input_name,
                provider=provider,
                secret_ref=validate_secret_ref(secret_ref, provider=provider),
                updated_by_principal_id=principal_id,
            )


def _apply_parameter_binding(step: dict[str, Any], binding: ParameterBinding) -> str | None:
    template = "{{ " + binding.input_name + " }}"
    operation_value = step.get("op")
    operation = operation_value if isinstance(operation_value, str) else None
    allowed_fields = {
        "navigate": {"url"},
        "fill": {"value"},
        "select": {"select_value"},
        "wait": {"text", "text_gone"},
    }
    if operation is None:
        return "workflow step is missing a valid operation"
    if binding.field not in allowed_fields.get(operation, set()):
        return f"field {binding.field!r} cannot parameterize a {operation!r} step"

    if binding.field == "select_value":
        values = step.get("values")
        assert binding.item_index is not None
        if not isinstance(values, list) or binding.item_index >= len(values):
            return f"select value index {binding.item_index} does not exist at step {binding.step}"
        values[binding.item_index] = template
        return None

    step[binding.field] = template
    return None
