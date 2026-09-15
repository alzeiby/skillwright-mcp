from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .browser import BrowserController
from .compiler import WorkflowCompilationError, compile_actions
from .db import Database
from .engine import WorkflowEngine
from .workflow import ParameterBinding, WorkflowDefinition, WorkflowInput


class SkillService:
    def __init__(
        self, database: Database, browser: BrowserController, engine: WorkflowEngine
    ) -> None:
        self.database = database
        self.browser = browser
        self.engine = engine

    async def record_start(self, name: str, description: str = "") -> dict[str, Any]:
        if self.browser.active_recording_id is not None:
            return {
                "status": "already_recording",
                "recording_id": self.browser.active_recording_id,
            }
        row = await self.database.start_recording(name=name, description=description)
        self.browser.active_recording_id = row.id
        await self.database.audit(
            "skill.recording.started",
            entity_type="recording",
            entity_id=row.id,
            data={"name": name},
        )
        return {"status": "recording", "recording_id": row.id, "name": name}

    async def record_stop(self) -> dict[str, Any]:
        recording_id = self.browser.active_recording_id
        if recording_id is None:
            return {"status": "not_recording"}
        self.browser.active_recording_id = None
        recording = await self.database.get_recording(recording_id)
        if recording is None:
            return {"status": "error", "error": "active recording disappeared"}
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
            recording_id=recording_id,
            change_reason="recording",
        )
        await self.database.stop_recording(recording_id, status="compiled")
        await self.database.audit(
            "skill.version.created",
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
    ) -> dict[str, Any]:
        actions = await self.database.action_range(start_event, end_event)
        if not actions:
            return {"status": "not_found", "error": "no browser actions in requested range"}
        try:
            workflow = compile_actions(name=name, description=description, actions=actions)
        except WorkflowCompilationError as exc:
            return {"status": "compile_failed", "error": str(exc)}
        skill, version = await self.database.create_skill_version(
            workflow,
            change_reason=f"history events {start_event}-{end_event}",
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

    async def rollback(self, name: str, version: int) -> dict[str, Any]:
        stored = await self.database.get_workflow_version(name, version)
        if stored is None:
            return {"status": "not_found", "skill": name, "version": version}
        skill, old_version = stored
        workflow = WorkflowDefinition.model_validate(old_version.definition)
        _, new_version = await self.database.create_skill_version(
            workflow,
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
            ).model_dump(mode="json")
            if existing is not None and existing != input_spec:
                return {
                    "status": "invalid_binding",
                    "error": f"input {binding.input_name!r} is already defined differently",
                }
            definition["inputs"][binding.input_name] = input_spec

        updated = WorkflowDefinition.model_validate(definition)
        try:
            _, new_version = await self.database.create_skill_version(
                updated,
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
