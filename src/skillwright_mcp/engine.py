from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from copy import deepcopy
from typing import Any

from .browser import BrowserActionResult, BrowserController
from .db import Database, SkillRow, WorkflowVersionRow
from .secrets import Redactor, SecretResolutionError, resolve_secret
from .snapshot import PageSnapshot, SnapshotElement, parse_snapshot
from .workflow import (
    AssertStep,
    ClickStep,
    ElementTarget,
    ExtractStep,
    FillStep,
    SelectStep,
    SkillCallStep,
    WaitStep,
    WorkflowDefinition,
    WorkflowStep,
    render_template,
    render_value,
)

BrowserFactory = Callable[[], BrowserController]


class WorkflowEngine:
    """Execute persisted workflows directly in-process without a run/worker state machine."""

    def __init__(
        self,
        database: Database,
        browser_factory: BrowserFactory,
    ) -> None:
        self.database = database
        self._browser_factory = browser_factory

    async def run_skill(
        self,
        name: str,
        *,
        inputs: dict[str, Any] | None = None,
        version: int | None = None,
    ) -> dict[str, Any]:
        stored = await self.database.get_workflow_version(name, version)
        if stored is None:
            return {"status": "not_found", "workflow": name, "version": version}
        skill, version_row = stored
        workflow = WorkflowDefinition.model_validate(version_row.definition)
        prepared = await self._prepare_variables(skill, workflow, inputs)
        if isinstance(prepared, dict):
            return prepared
        variables, redactor = prepared

        async with self._browser() as browser:
            return await self._execute_workflow(
                browser=browser,
                version_row=version_row,
                workflow=workflow,
                variables=variables,
                redactor=redactor,
                stack=((skill.name, version_row.version),),
            )

    async def repair_skill(
        self,
        name: str,
        *,
        base_version: int,
        step: int | None = None,
        replacement_target: ElementTarget | None = None,
        replacement_step: WorkflowStep | None = None,
        replacement_workflow: WorkflowDefinition | None = None,
        inputs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Replay-validate an agent-proposed patch, then persist a new immutable version."""

        stored = await self.database.get_workflow_version(name, base_version)
        if stored is None:
            return {"status": "not_found", "workflow": name, "version": base_version}
        skill, version_row = stored
        if skill.current_version != base_version:
            return {
                "status": "conflict",
                "workflow": name,
                "error": (
                    f"repair base v{base_version} is stale; "
                    f"current is v{skill.current_version}"
                ),
            }
        workflow = WorkflowDefinition.model_validate(version_row.definition)
        choices = [
            replacement_target is not None,
            replacement_step is not None,
            replacement_workflow is not None,
        ]
        if sum(choices) != 1:
            return {
                "status": "invalid_repair",
                "error": (
                    "provide exactly one of replacement_target, replacement_step, "
                    "or replacement_workflow"
                ),
            }

        if replacement_workflow is not None:
            if replacement_workflow.name != name:
                return {
                    "status": "invalid_repair",
                    "error": "replacement_workflow must keep the repaired skill name",
                }
            patched = replacement_workflow
            repair_reason = "agent candidate workflow repair"
            repaired_step: int | None = None
        else:
            if step is None or step < 0 or step >= len(workflow.steps):
                return {"status": "invalid_repair", "error": f"step {step} does not exist"}
            selected = workflow.steps[step]
            definition = deepcopy(workflow.model_dump(mode="json"))
            if replacement_step is not None:
                definition["steps"][step] = replacement_step.model_dump(mode="json")
                repair_reason = f"agent replacement of step {step}"
            else:
                if not hasattr(selected, "target") or not isinstance(
                    getattr(selected, "target", None), ElementTarget
                ):
                    return {
                        "status": "invalid_repair",
                        "error": f"step {step} ({selected.op}) has no repairable element target",
                    }
                assert replacement_target is not None
                definition["steps"][step]["target"] = replacement_target.model_dump(mode="json")
                repair_reason = f"agent target repair of step {step}"
            try:
                patched = WorkflowDefinition.model_validate(definition)
            except ValueError as exc:
                return {
                    "status": "invalid_repair",
                    "error": str(exc),
                }
            repaired_step = step
        prepared = await self._prepare_variables(skill, patched, inputs)
        if isinstance(prepared, dict):
            return prepared
        variables, redactor = prepared

        async with self._browser() as browser:
            validation = await self._execute_workflow(
                browser=browser,
                version_row=version_row,
                workflow=patched,
                variables=variables,
                redactor=redactor,
                stack=((skill.name, version_row.version),),
            )
        if validation.get("status") != "succeeded":
            return {
                "status": "repair_validation_failed",
                "workflow": name,
                "base_version": base_version,
                "validation": validation,
            }

        try:
            _, new_version = await self.database.create_skill_version(
                patched,
                parent_version=base_version,
                change_reason=repair_reason,
                expected_current_version=base_version,
            )
        except ValueError as exc:
            return {"status": "conflict", "workflow": name, "error": str(exc)}
        return {
            "status": "saved",
            "workflow": name,
            "validated": True,
            "repaired_step": repaired_step,
            "base_version": base_version,
            "version": new_version.version,
            "tool": skill.tool_name,
            "validation": validation,
        }

    @asynccontextmanager
    async def _browser(self) -> AsyncIterator[BrowserController]:
        browser = self._browser_factory()
        try:
            yield browser
        finally:
            await browser.close()

    async def _prepare_variables(
        self,
        skill: SkillRow,
        workflow: WorkflowDefinition,
        inputs: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], Redactor] | dict[str, Any]:
        try:
            variables = workflow.prepare_inputs(inputs)
        except ValueError as exc:
            return {"status": "invalid_inputs", "workflow": workflow.name, "error": str(exc)}

        secret_bindings = await self.database.skill_secret_bindings(skill.id)
        secret_values: list[str] = []
        for input_name, input_spec in workflow.inputs.items():
            if not input_spec.secret:
                continue
            secret_ref = secret_bindings.get(input_name)
            if secret_ref is None:
                return {
                    "status": "secret_unavailable",
                    "workflow": workflow.name,
                    "input": input_name,
                    "reason": "secret_binding_missing",
                    "side_effect_state": "not_started",
                }
            try:
                value = resolve_secret(secret_ref)
            except SecretResolutionError:
                return {
                    "status": "secret_unavailable",
                    "workflow": workflow.name,
                    "input": input_name,
                    "reason": "secret_value_missing",
                    "side_effect_state": "not_started",
                }
            variables[input_name] = value
            secret_values.append(value)
        return variables, Redactor.from_values(secret_values)

    async def _execute_workflow(
        self,
        *,
        browser: BrowserController,
        version_row: WorkflowVersionRow,
        workflow: WorkflowDefinition,
        variables: dict[str, Any],
        redactor: Redactor,
        stack: tuple[tuple[str, int], ...],
    ) -> dict[str, Any]:
        outputs: dict[str, Any] = {}
        side_effect_state = "not_started"
        for index, step in enumerate(workflow.steps):
            if isinstance(step, SkillCallStep):
                try:
                    child = await self._execute_skill_call(
                        browser=browser,
                        step=step,
                        parent_variables=variables,
                        redactor=redactor,
                        stack=stack,
                    )
                except (TypeError, ValueError) as exc:
                    snapshot = await self._capture_snapshot(browser, redactor=redactor)
                    return self._repair_context(
                        workflow=workflow,
                        version_row=version_row,
                        step_index=index,
                        step=step,
                        snapshot=snapshot,
                        reason="step_error",
                        error=str(exc),
                        side_effect_state=side_effect_state,
                        current_step_side_effect_state="not_started",
                    )
                if child.get("status") != "succeeded":
                    child_state = str(child.get("side_effect_state", "unknown"))
                    return {
                        "status": child.get("status", "failed"),
                        "workflow": workflow.name,
                        "workflow_version": version_row.version,
                        "step": index,
                        "operation": "skill",
                        "child": child,
                        "failure_path": [
                            {
                                "workflow": workflow.name,
                                "version": version_row.version,
                                "step": index,
                            },
                            *list(child.get("failure_path", [])),
                        ],
                        "side_effect_state": _combine_side_effect_state(
                            side_effect_state,
                            child_state,
                        ),
                    }
                side_effect_state = _combine_side_effect_state(
                    side_effect_state,
                    str(child.get("side_effect_state", "not_started")),
                )
                child_outputs = child.get("outputs", {})
                for child_name, parent_name in step.outputs.items():
                    if child_name not in child_outputs:
                        return {
                            "status": "failed",
                            "workflow": workflow.name,
                            "workflow_version": version_row.version,
                            "step": index,
                            "operation": "skill",
                            "error": (
                                f"child skill {step.skill!r} did not produce mapped output "
                                f"{child_name!r}"
                            ),
                            "side_effect_state": "unknown",
                        }
                    value = child_outputs[child_name]
                    variables[parent_name] = value
                    outputs[parent_name] = value
                continue

            try:
                execution = await self._execute_step(
                    browser,
                    step,
                    variables,
                    redactor=redactor,
                )
            except (TypeError, ValueError) as exc:
                snapshot = await self._capture_snapshot(browser, redactor=redactor)
                return self._repair_context(
                    workflow=workflow,
                    version_row=version_row,
                    step_index=index,
                    step=step,
                    snapshot=snapshot,
                    reason="step_error",
                    error=str(exc),
                    side_effect_state=side_effect_state,
                    current_step_side_effect_state="not_started",
                )
            if execution["status"] == "repair_required":
                current_step_state = str(execution.get("side_effect_state", "not_started"))
                return self._repair_context(
                    workflow=workflow,
                    version_row=version_row,
                    step_index=index,
                    step=step,
                    snapshot=execution["snapshot"],
                    reason=execution["reason"],
                    side_effect_state=_combine_side_effect_state(
                        side_effect_state,
                        current_step_state,
                    ),
                    current_step_side_effect_state=current_step_state,
                )
            if execution["status"] == "failed":
                current_step_state = str(execution.get("side_effect_state", "not_started"))
                failure_snapshot = await self._capture_snapshot(browser, redactor=redactor)
                return self._repair_context(
                    workflow=workflow,
                    version_row=version_row,
                    step_index=index,
                    step=step,
                    snapshot=failure_snapshot,
                    reason="step_failed",
                    error=str(execution["error"]),
                    failure=execution,
                    side_effect_state=_combine_side_effect_state(
                        side_effect_state,
                        current_step_state,
                    ),
                    current_step_side_effect_state=current_step_state,
                )
            side_effect_state = _combine_side_effect_state(
                side_effect_state,
                str(execution.get("side_effect_state", "not_started")),
            )
            if "output" in execution:
                output_name, output_value = execution["output"]
                outputs[output_name] = output_value
                variables[output_name] = output_value

        declared_outputs: dict[str, Any] = {}
        for output_name, spec in workflow.outputs.items():
            source = spec.source or output_name
            if source not in variables:
                return {
                    "status": "failed",
                    "workflow": workflow.name,
                    "workflow_version": version_row.version,
                    "error": f"declared output {output_name!r} source {source!r} was not produced",
                    "side_effect_state": "unknown",
                }
            value = variables[source]
            if not _matches_output_type(value, spec.type):
                return {
                    "status": "failed",
                    "workflow": workflow.name,
                    "workflow_version": version_row.version,
                    "error": (
                        f"declared output {output_name!r} expected {spec.type!r} "
                        f"but produced {type(value).__name__}"
                    ),
                    "side_effect_state": side_effect_state,
                }
            declared_outputs[output_name] = value

        return {
            "status": "succeeded",
            "workflow": workflow.name,
            "workflow_version": version_row.version,
            "steps_completed": len(workflow.steps),
            "outputs": declared_outputs if workflow.outputs else outputs,
            "side_effect_state": side_effect_state,
        }

    async def _execute_skill_call(
        self,
        *,
        browser: BrowserController,
        step: SkillCallStep,
        parent_variables: dict[str, Any],
        redactor: Redactor,
        stack: tuple[tuple[str, int], ...],
    ) -> dict[str, Any]:
        key = (step.skill, step.version)
        if key in stack:
            return {
                "status": "failed",
                "workflow": step.skill,
                "workflow_version": step.version,
                "error": "composition cycle detected at runtime",
                "side_effect_state": "not_started",
            }
        stored = await self.database.get_workflow_version(step.skill, step.version)
        if stored is None:
            return {
                "status": "failed",
                "workflow": step.skill,
                "workflow_version": step.version,
                "error": "pinned child skill version no longer exists",
                "side_effect_state": "not_started",
            }
        skill, version_row = stored
        workflow = WorkflowDefinition.model_validate(version_row.definition)
        mapped_inputs = {
            name: render_value(value, parent_variables) for name, value in step.inputs.items()
        }
        prepared = await self._prepare_variables(skill, workflow, mapped_inputs)
        if isinstance(prepared, dict):
            return prepared
        variables, child_redactor = prepared
        return await self._execute_workflow(
            browser=browser,
            version_row=version_row,
            workflow=workflow,
            variables=variables,
            redactor=redactor.merged(child_redactor),
            stack=(*stack, key),
        )

    async def _execute_step(
        self,
        browser: BrowserController,
        step: WorkflowStep,
        variables: dict[str, Any],
        *,
        redactor: Redactor,
    ) -> dict[str, Any]:
        if isinstance(step, SkillCallStep):
            raise TypeError("skill-call steps are executed by the composition layer")
        if step.op == "navigate":
            action = await browser.navigate(
                render_template(step.url, variables),
                redactor=redactor,
            )
            return _action_outcome(action, mutating=False)
        if isinstance(step, WaitStep):
            action = await browser.wait(
                seconds=step.seconds,
                text=render_template(step.text, variables) if step.text else None,
                text_gone=render_template(step.text_gone, variables) if step.text_gone else None,
                redactor=redactor,
            )
            return _action_outcome(action, mutating=False)

        snapshot_action = await browser.snapshot(redactor=redactor)
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
                    browser,
                    step.target,
                    snapshot,
                    redactor=redactor,
                )
                if resolved is None:
                    return {
                        "status": "repair_required",
                        "reason": "target_unresolved",
                        "snapshot": snapshot,
                    }
            return _execute_assert(step, snapshot, variables)
        if isinstance(step, ExtractStep):
            element = await self._resolve_target(
                browser,
                step.target,
                snapshot,
                redactor=redactor,
            )
            if element is None:
                return {
                    "status": "repair_required",
                    "reason": "target_unresolved",
                    "snapshot": snapshot,
                }
            if step.attribute is None:
                value: Any = element.name
            else:
                value = element.attributes.get(step.attribute)
                if value is None:
                    return {
                        "status": "failed",
                        "error": f"attribute {step.attribute!r} is not present on resolved element",
                        "side_effect_state": "not_started",
                        "resolved_target": _resolved_element(element),
                    }
            coerced = _coerce_output(value, step.output_type)
            if isinstance(coerced, ValueError):
                return {
                    "status": "failed",
                    "error": str(coerced),
                    "side_effect_state": "not_started",
                }
            return {
                "status": "succeeded",
                "output": (step.save_as, coerced),
            }

        target = step.target
        element = await self._resolve_target(browser, target, snapshot, redactor=redactor)
        if element is None:
            return {
                "status": "repair_required",
                "reason": "target_unresolved",
                "snapshot": snapshot,
            }

        if isinstance(step, ClickStep):
            action = await browser.click(
                element.ref,
                element=target.recorded_description or element.name,
                double_click=step.double_click,
                button=step.button,
                redactor=redactor,
            )
            outcome = _action_outcome(action, mutating=True)
        elif isinstance(step, FillStep):
            action = await browser.fill(
                element.ref,
                render_template(step.value, variables),
                element=target.recorded_description or element.name,
                submit=step.submit,
                redactor=redactor,
            )
            outcome = _action_outcome(action, mutating=True)
        elif isinstance(step, SelectStep):
            action = await browser.select(
                element.ref,
                [render_template(value, variables) for value in step.values],
                element=target.recorded_description or element.name,
                redactor=redactor,
            )
            outcome = _action_outcome(action, mutating=True)
        else:  # pragma: no cover - discriminated union keeps this exhaustive
            raise TypeError(f"unsupported workflow step: {type(step).__name__}")
        if outcome["status"] != "succeeded":
            outcome["resolved_target"] = _resolved_element(element)
        return outcome

    async def _resolve_target(
        self,
        browser: BrowserController,
        target: ElementTarget,
        full_snapshot: PageSnapshot,
        *,
        redactor: Redactor,
    ) -> SnapshotElement | None:
        if target.locator:
            locator_snapshot = await browser.snapshot(
                target=target.locator,
                depth=2,
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
        workflow: WorkflowDefinition,
        version_row: WorkflowVersionRow,
        step_index: int,
        step: WorkflowStep,
        snapshot: PageSnapshot | None,
        reason: str,
        side_effect_state: str,
        current_step_side_effect_state: str,
        error: str | None = None,
        failure: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        target = getattr(step, "target", None)
        candidates = []
        if isinstance(target, ElementTarget) and snapshot is not None:
            for candidate_index, (element, score) in enumerate(snapshot.ranked_candidates(target)):
                candidates.append(
                    {
                        "id": f"candidate_{candidate_index}",
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
        result: dict[str, Any] = {
            "status": "repair_required",
            "workflow": workflow.name,
            "workflow_version": version_row.version,
            "step": step_index,
            "operation": step.op,
            "reason": reason,
            "expected_step": step.model_dump(mode="json"),
            "expected": (
                target.model_dump(mode="json") if isinstance(target, ElementTarget) else None
            ),
            "page": (
                {"url": snapshot.url, "title": snapshot.title} if snapshot is not None else None
            ),
            "snapshot": snapshot.raw if snapshot is not None else None,
            "candidates": candidates,
            "side_effect_state": side_effect_state,
            "current_step_side_effect_state": current_step_side_effect_state,
            "repair": {
                "tool": "skill_repair",
                "instructions": (
                    "Inspect the evidence or use browser_* tools, then call skill_repair with "
                    "this workflow/version and exactly one repair: a complete "
                    "replacement_workflow, or this step plus replacement_target or "
                    "replacement_step. The candidate workflow is replay-validated before a new "
                    "version is saved."
                ),
            },
        }
        if error is not None:
            result["error"] = error
        if failure is not None:
            result["failure"] = failure
        return result

    async def _capture_snapshot(
        self,
        browser: BrowserController,
        *,
        redactor: Redactor,
    ) -> PageSnapshot | None:
        snapshot_action = await browser.snapshot(redactor=redactor)
        if not snapshot_action.ok or snapshot_action.result is None:
            return None
        return parse_snapshot(snapshot_action.result.text)


def _execute_assert(
    step: AssertStep,
    snapshot: PageSnapshot,
    variables: dict[str, Any],
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
    return {"status": "succeeded"}


def _action_outcome(action: BrowserActionResult, *, mutating: bool) -> dict[str, Any]:
    if action.ok:
        return {
            "status": "succeeded",
            "side_effect_state": "completed" if mutating else "not_started",
        }
    return {
        "status": "failed",
        "error": action.error or "browser action failed",
        "result": action.result.as_dict() if action.result is not None else None,
        "side_effect_state": "unknown" if mutating else "not_started",
    }


def _combine_side_effect_state(previous: str, current: str) -> str:
    if "unknown" in {previous, current}:
        return "unknown"
    if "completed" in {previous, current}:
        return "completed"
    return "not_started"


def _resolved_element(element: SnapshotElement) -> dict[str, Any]:
    return {"runtime_ref": element.ref, "role": element.role, "name": element.name}


def _coerce_output(value: Any, output_type: str) -> Any | ValueError:
    if output_type == "string":
        return "" if value is None else str(value)
    if output_type == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.casefold() in {"true", "false"}:
            return value.casefold() == "true"
        return ValueError("extracted value is not a boolean")
    if output_type == "integer":
        try:
            return int(value)
        except (TypeError, ValueError):
            return ValueError("extracted value is not an integer")
    try:
        return float(value)
    except (TypeError, ValueError):
        return ValueError("extracted value is not a number")


def _matches_output_type(value: Any, output_type: str) -> bool:
    if output_type == "string":
        return isinstance(value, str)
    if output_type == "boolean":
        return isinstance(value, bool)
    if output_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, int | float) and not isinstance(value, bool)
