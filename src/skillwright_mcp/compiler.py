from __future__ import annotations

from collections.abc import Sequence

from pydantic import ValidationError

from .db import BrowserActionRow
from .snapshot import parse_snapshot
from .workflow import (
    ClickStep,
    ElementTarget,
    FillStep,
    NavigateStep,
    SelectStep,
    WaitStep,
    WorkflowDefinition,
    WorkflowInput,
    WorkflowStep,
)


class WorkflowCompilationError(ValueError):
    pass


def compile_actions(
    *,
    name: str,
    description: str,
    actions: Sequence[BrowserActionRow],
) -> WorkflowDefinition:
    steps: list[WorkflowStep] = []
    inputs: dict[str, WorkflowInput] = {}
    for action in actions:
        if not action.success:
            continue
        args = action.arguments
        if action.tool_name == "browser_snapshot":
            continue
        if action.tool_name == "browser_navigate":
            steps.append(NavigateStep(url=str(args["url"])))
        elif action.tool_name == "browser_click":
            button = args.get("button", "left")
            if button not in {"left", "right", "middle"}:
                raise WorkflowCompilationError(
                    f"event {action.id} has unsupported click button {button!r}"
                )
            steps.append(
                ClickStep(
                    target=_compile_target(action),
                    double_click=bool(args.get("double_click", False)),
                    button=button,
                )
            )
        elif action.tool_name == "browser_fill":
            steps.append(
                FillStep(
                    target=_compile_target(action),
                    value=str(args["text"]),
                    submit=bool(args.get("submit", False)),
                )
            )
        elif action.tool_name == "browser_fill_secret":
            input_name = action.arguments.get("input_name")
            if not isinstance(input_name, str) or not input_name:
                raise WorkflowCompilationError(f"event {action.id} is missing a secret input name")
            secret_input = WorkflowInput(type="string", required=True, secret=True)
            inputs[input_name] = secret_input
            steps.append(
                FillStep(
                    target=_compile_target(action),
                    value="{{ " + input_name + " }}",
                    submit=bool(args.get("submit", False)),
                )
            )
        elif action.tool_name == "browser_select":
            steps.append(
                SelectStep(
                    target=_compile_target(action),
                    values=[str(value) for value in args.get("values", [])],
                )
            )
        elif action.tool_name == "browser_wait":
            steps.append(
                WaitStep(
                    seconds=args.get("seconds"),
                    text=args.get("text"),
                    text_gone=args.get("text_gone"),
                )
            )

    if not steps:
        raise WorkflowCompilationError("recording contains no successful workflow actions")
    try:
        return WorkflowDefinition(
            name=name,
            description=description,
            inputs=inputs,
            steps=steps,
        )
    except ValidationError as exc:
        raise WorkflowCompilationError(f"recorded workflow is invalid: {exc}") from exc


def _compile_target(action: BrowserActionRow) -> ElementTarget:
    raw_target = str(action.arguments.get("target", ""))
    description = action.arguments.get("element")
    if action.snapshot_before:
        snapshot = parse_snapshot(action.snapshot_before)
        element = snapshot.by_ref(raw_target)
        if element is not None:
            return element.to_target(
                description=str(description) if description else None,
                page_url=snapshot.url,
                locator=action.durable_locator,
            )

    # Saving a transient Playwright ref without semantic evidence would create a brittle skill.
    if _looks_like_snapshot_ref(raw_target):
        raise WorkflowCompilationError(
            f"event {action.id} uses transient target {raw_target!r} "
            "but has no matching snapshot evidence"
        )
    fallback = str(description or raw_target).strip()
    if not fallback:
        raise WorkflowCompilationError(f"event {action.id} has no durable target evidence")
    return ElementTarget(locator=action.durable_locator, recorded_description=fallback)


def _looks_like_snapshot_ref(value: str) -> bool:
    if value.startswith("e") and value[1:].isdigit():
        return True
    if value.startswith("f") and "e" in value:
        frame, _, element = value.partition("e")
        return frame[1:].isdigit() and element.isdigit()
    return False
