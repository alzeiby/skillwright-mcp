from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .browser import BrowserController
from .compiler import WorkflowCompilationError, compile_actions
from .db import Database
from .secrets import validate_secret_ref
from .workflow import (
    ElementTarget,
    ExtractStep,
    ParameterBinding,
    PrimitiveType,
    PrimitiveValue,
    SkillCallStep,
    WorkflowDefinition,
    WorkflowInput,
    WorkflowOutput,
    WorkflowStep,
    render_value,
    template_name,
    template_names,
)


class CompositionCall(BaseModel):
    """One child skill reference used when authoring a composed skill."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    skill: str = Field(min_length=1, max_length=160)
    version: int | None = Field(default=None, ge=1)
    inputs: dict[str, PrimitiveValue] = Field(default_factory=dict)
    outputs: dict[str, str] = Field(default_factory=dict)


class SkillService:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def record_start(
        self,
        browser: BrowserController,
        name: str,
        description: str = "",
    ) -> dict[str, Any]:
        if browser.active_recording is not None:
            return {"status": "already_recording"}
        start_event = await self.database.latest_action_id(browser.history_scope)
        browser.active_recording = (name, description, start_event)
        return {"status": "recording", "name": name}

    async def record_stop(self, browser: BrowserController) -> dict[str, Any]:
        recording = browser.active_recording
        if recording is None:
            return {"status": "not_recording"}
        browser.active_recording = None
        name, description, start_event = recording
        end_event = await self.database.latest_action_id(browser.history_scope)
        actions = (
            await self.database.action_range(
                start_event + 1,
                end_event,
                history_scope=browser.history_scope,
            )
            if end_event > start_event
            else []
        )
        return await self._save_actions(
            name,
            description,
            actions,
            change_reason="recording",
        )

    async def save_from_history(
        self,
        name: str,
        *,
        start_event: int,
        end_event: int,
        history_scope: str,
        description: str = "",
    ) -> dict[str, Any]:
        actions = await self.database.action_range(
            start_event,
            end_event,
            history_scope=history_scope,
        )
        if not actions:
            return {"status": "not_found", "error": "no browser actions in requested range"}
        return await self._save_actions(
            name,
            description,
            actions,
            change_reason=f"history events {start_event}-{end_event}",
        )

    async def _save_actions(
        self,
        name: str,
        description: str,
        actions: Sequence[Any],
        *,
        change_reason: str,
    ) -> dict[str, Any]:
        try:
            workflow = compile_actions(name=name, description=description, actions=actions)
            secret_bindings = _recorded_secret_bindings(actions)
        except WorkflowCompilationError as exc:
            return {"status": "compile_failed", "error": str(exc)}
        skill, version = await self.database.create_skill_version(
            workflow,
            change_reason=change_reason,
            secret_bindings=secret_bindings,
        )
        return {
            "status": "saved",
            "skill": skill.name,
            "tool": skill.tool_name,
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
                    "tool": row.tool_name,
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
                    "tool": row.tool_name,
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
            "tool": skill.tool_name,
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
            "tool": skill.tool_name,
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
        try:
            _, new_version = await self.database.create_skill_version(
                workflow,
                parent_version=skill.current_version,
                change_reason=f"rollback to v{version}",
                expected_current_version=skill.current_version,
            )
        except ValueError as exc:
            return {"status": "conflict", "error": str(exc)}
        return {
            "status": "saved",
            "skill": name,
            "tool": skill.tool_name,
            "rolled_back_to": version,
            "new_version": new_version.version,
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
                return {"status": "invalid_binding", "error": f"step {binding.step} does not exist"}
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

        definition["schema_version"] = 4
        try:
            updated = WorkflowDefinition.model_validate(definition)
        except ValueError as exc:
            return {"status": "invalid_binding", "error": str(exc)}
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
            "tool": skill.tool_name,
            "version": new_version.version,
            "inputs": updated.model_dump(mode="json")["inputs"],
            "workflow": updated.model_dump(mode="json"),
        }

    async def add_output(
        self,
        name: str,
        *,
        output_name: str,
        target: ElementTarget,
        output_type: PrimitiveType = "string",
        attribute: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        stored = await self.database.get_workflow_version(name)
        if stored is None:
            return {"status": "not_found", "skill": name}
        skill, version = stored
        workflow = WorkflowDefinition.model_validate(version.definition)
        if output_name in workflow.outputs:
            return {
                "status": "invalid_output",
                "error": f"output {output_name!r} already exists",
            }
        if output_name in workflow.inputs:
            return {
                "status": "invalid_output",
                "error": f"output {output_name!r} conflicts with an input name",
            }
        try:
            extraction = ExtractStep(
                target=target,
                save_as=output_name,
                attribute=attribute,
                output_type=output_type,
            )
            output = WorkflowOutput(
                type=output_type,
                description=description,
                source=output_name,
            )
            updated = workflow.model_copy(
                update={
                    "schema_version": 4,
                    "steps": [*workflow.steps, extraction],
                    "outputs": {**workflow.outputs, output_name: output},
                }
            )
            updated = WorkflowDefinition.model_validate(updated.model_dump(mode="json"))
        except ValueError as exc:
            return {"status": "invalid_output", "error": str(exc)}
        try:
            _, new_version = await self.database.create_skill_version(
                updated,
                parent_version=version.version,
                change_reason=f"declared output {output_name}",
                expected_current_version=version.version,
            )
        except ValueError as exc:
            return {"status": "conflict", "error": str(exc)}
        return {
            "status": "saved",
            "skill": skill.name,
            "tool": skill.tool_name,
            "version": new_version.version,
            "output": output_name,
            "workflow": updated.model_dump(mode="json"),
        }

    async def bind_secret(
        self,
        name: str,
        *,
        input_name: str,
        secret_ref: str,
    ) -> dict[str, Any]:
        stored = await self.database.get_workflow_version(name)
        if stored is None:
            return {"status": "not_found", "skill": name}
        skill, version = stored
        workflow = WorkflowDefinition.model_validate(version.definition)
        input_spec = workflow.inputs.get(input_name)
        if input_spec is None or not input_spec.secret:
            return {"status": "invalid_secret_input", "skill": name, "input": input_name}
        try:
            normalized_ref = validate_secret_ref(secret_ref)
        except ValueError as exc:
            return {"status": "invalid_secret_ref", "error": str(exc)}
        await self.database.bind_skill_secret(
            skill_id=skill.id,
            input_name=input_name,
            secret_ref=normalized_ref,
        )
        return {
            "status": "bound",
            "skill": name,
            "input": input_name,
            "configured": True,
        }

    async def unbind_secret(self, name: str, *, input_name: str) -> dict[str, Any]:
        skill = await self.database.get_skill(name)
        if skill is None:
            return {"status": "not_found", "skill": name}
        removed = await self.database.unbind_skill_secret(skill_id=skill.id, input_name=input_name)
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
        bindings = await self.database.skill_secret_bindings(skill.id)
        return {
            "status": "found",
            "skill": name,
            "secrets": [
                {
                    "input": input_name,
                    "configured": input_name in bindings,
                }
                for input_name, spec in workflow.inputs.items()
                if spec.secret
            ],
        }

    async def compose(
        self,
        name: str,
        calls: Sequence[CompositionCall],
        *,
        description: str = "",
        inputs: dict[str, WorkflowInput] | None = None,
        outputs: dict[str, WorkflowOutput] | None = None,
    ) -> dict[str, Any]:
        if not calls:
            return {
                "status": "invalid_composition",
                "error": "composition needs at least one skill",
            }

        parent_inputs = dict(inputs or {})
        parent_outputs = dict(outputs or {})
        available_types: dict[str, str] = {
            input_name: spec.type for input_name, spec in parent_inputs.items()
        }
        nullable_values = {
            input_name
            for input_name, spec in parent_inputs.items()
            if not spec.required and spec.default is None
        }
        pinned: list[WorkflowStep] = []
        for index, call in enumerate(calls):
            stored = await self.database.get_workflow_version(call.skill, call.version)
            if stored is None:
                return {
                    "status": "invalid_composition",
                    "error": f"child skill {call.skill!r} version {call.version!r} was not found",
                }
            _child_skill, child_version = stored
            child = WorkflowDefinition.model_validate(child_version.definition)
            validation_error = _validate_child_inputs(
                child,
                call.inputs,
                available_types=available_types,
                nullable_values=nullable_values,
            )
            if validation_error:
                return {
                    "status": "invalid_composition",
                    "step": index,
                    "error": validation_error,
                }
            for child_output, parent_output in call.outputs.items():
                spec = child.outputs.get(child_output)
                if spec is None:
                    return {
                        "status": "invalid_composition",
                        "step": index,
                        "error": (
                            f"child skill {call.skill!r} has no declared output "
                            f"{child_output!r}"
                        ),
                    }
                previous = available_types.get(parent_output)
                if previous is not None and previous != spec.type:
                    return {
                        "status": "invalid_composition",
                        "step": index,
                        "error": (
                            f"output {parent_output!r} has incompatible types "
                            f"{previous!r} and {spec.type!r}"
                        ),
                    }
                available_types[parent_output] = spec.type
                nullable_values.discard(parent_output)
                parent_outputs.setdefault(
                    parent_output,
                    WorkflowOutput(
                        type=spec.type,
                        description=spec.description,
                        source=parent_output,
                    ),
                )
            pinned.append(
                SkillCallStep(
                    skill=call.skill,
                    version=child_version.version,
                    inputs=call.inputs,
                    outputs=call.outputs,
                )
            )

        for output_name, spec in parent_outputs.items():
            source = spec.source or output_name
            if source not in available_types:
                return {
                    "status": "invalid_composition",
                    "error": f"output {output_name!r} references unavailable value {source!r}",
                }
            if source in nullable_values:
                return {
                    "status": "invalid_composition",
                    "error": (
                        f"output {output_name!r} references nullable value {source!r}; "
                        "declared outputs must be non-null"
                    ),
                }
            if not _type_assignable(available_types[source], spec.type):
                return {
                    "status": "invalid_composition",
                    "error": f"output {output_name!r} type does not match source {source!r}",
                }

        workflow = WorkflowDefinition(
            name=name,
            description=description,
            inputs=parent_inputs,
            outputs=parent_outputs,
            steps=pinned,
        )
        current = await self.database.get_skill(name)
        expected = current.current_version if current is not None else 0
        try:
            skill, version = await self.database.create_skill_version(
                workflow,
                parent_version=expected or None,
                change_reason="composed from saved skills",
                expected_current_version=expected,
            )
        except ValueError as exc:
            return {"status": "conflict", "error": str(exc)}
        return {
            "status": "saved",
            "skill": skill.name,
            "tool": skill.tool_name,
            "version": version.version,
            "workflow": workflow.model_dump(mode="json"),
        }

def _validate_child_inputs(
    child: WorkflowDefinition,
    bindings: dict[str, PrimitiveValue],
    *,
    available_types: Mapping[str, str],
    nullable_values: set[str],
) -> str | None:
    unknown = sorted(set(bindings) - set(child.inputs))
    if unknown:
        return f"unknown inputs for child skill {child.name!r}: {', '.join(unknown)}"
    for input_name, spec in child.inputs.items():
        if spec.secret:
            if input_name in bindings:
                return f"secret child input {input_name!r} is server-bound and cannot be mapped"
            continue
        if input_name not in bindings:
            if spec.required and spec.default is None:
                return f"missing required child input {input_name!r} for skill {child.name!r}"
            continue
        value = bindings[input_name]
        if isinstance(value, str):
            names = template_names(value)
            missing = sorted(names - set(available_types))
            if missing:
                return (
                    f"child input {input_name!r} references unavailable values: "
                    f"{', '.join(missing)}"
                )
            source = template_name(value)
            if source is not None:
                if not _type_assignable(available_types[source], spec.type):
                    return (
                        f"child input {input_name!r} expects {spec.type!r} but "
                        f"{source!r} is {available_types[source]!r}"
                    )
                if source in nullable_values and spec.required:
                    return (
                        f"child input {input_name!r} is required but source {source!r} "
                        "may be null"
                    )
        try:
            rendered = render_value(
                value, {name: _sample_for_type(kind) for name, kind in available_types.items()}
            )
            WorkflowDefinition(
                name="input_validation",
                inputs={input_name: spec.model_copy(update={"secret": False})},
                steps=[SkillCallStep(skill="placeholder", version=1)],
            ).prepare_inputs({input_name: rendered})
        except ValueError as exc:
            return str(exc)
    return None


def _recorded_secret_bindings(actions: Sequence[Any]) -> dict[str, str]:
    seen: dict[str, str] = {}
    for action in actions:
        if action.tool_name != "browser_fill_secret":
            continue
        input_name = action.arguments.get("input_name")
        secret_ref = action.arguments.get("secret_ref")
        provider = action.arguments.get("provider", "env")
        binding_parts = (input_name, secret_ref, provider)
        if not all(isinstance(value, str) and value for value in binding_parts):
            raise WorkflowCompilationError(
                f"event {action.id} has incomplete secret binding metadata"
            )
        assert isinstance(input_name, str)
        assert isinstance(secret_ref, str)
        assert isinstance(provider, str)
        if provider != "env":
            raise WorkflowCompilationError(
                f"event {action.id} has an unsupported secret binding"
            )
        try:
            normalized_ref = validate_secret_ref(secret_ref)
        except ValueError as exc:
            raise WorkflowCompilationError(
                f"event {action.id} has an invalid secret binding"
            ) from exc
        previous = seen.get(input_name)
        if previous is not None and previous != normalized_ref:
            raise WorkflowCompilationError(
                f"recording uses multiple secret bindings for input {input_name!r}"
            )
        seen[input_name] = normalized_ref
    return seen


def _sample_for_type(kind: str) -> PrimitiveValue:
    if kind == "string":
        return "value"
    if kind == "number":
        return 1.5
    if kind == "integer":
        return 1
    return True


def _type_assignable(source: str, target: str) -> bool:
    return source == target or (source == "integer" and target == "number")


def _apply_parameter_binding(step: dict[str, Any], binding: ParameterBinding) -> str | None:
    template = "{{ " + binding.input_name + " }}"
    operation = step["op"]
    allowed_fields = {
        "navigate": {"url"},
        "fill": {"value"},
        "select": {"select_value"},
        "wait": {"text", "text_gone"},
    }
    if binding.field not in allowed_fields.get(operation, set()):
        return f"field {binding.field!r} cannot parameterize a {operation!r} step"

    if binding.field == "select_value":
        values = step["values"]
        assert binding.item_index is not None
        if binding.item_index >= len(values):
            return f"select value index {binding.item_index} does not exist at step {binding.step}"
        values[binding.item_index] = template
        return None

    step[binding.field] = template
    return None
