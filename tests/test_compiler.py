from __future__ import annotations

import pytest

from skillwright_mcp.compiler import WorkflowCompilationError, compile_actions
from skillwright_mcp.db import BrowserActionRow

SNAPSHOT = """\
### Page
- Page URL: https://example.test/billing
- Page Title: Billing
### Snapshot
- textbox "Account number" [ref=e2]
- button "Current Bill" [ref=e3]
"""


def action(**overrides: object) -> BrowserActionRow:
    values: dict[str, object] = {
        "id": 7,
        "recording_id": "recording",
        "run_id": None,
        "source": "agent",
        "tool_name": "browser_click",
        "arguments": {"target": "e3", "element": "Current Bill"},
        "upstream_tool_name": "browser_click",
        "upstream_arguments": {"target": "e3", "element": "Current Bill"},
        "durable_locator": "getByRole('button', { name: 'Current Bill' })",
        "state": "succeeded",
        "result": {},
        "success": True,
        "error": None,
        "snapshot_before": SNAPSHOT,
        "snapshot_after": None,
        "duration_ms": 1.0,
    }
    values.update(overrides)
    return BrowserActionRow(**values)


def test_compiler_replaces_runtime_ref_with_durable_target() -> None:
    workflow = compile_actions(name="invoice", description="", actions=[action()])
    step = workflow.steps[0]
    assert step.op == "click"
    assert step.target.role == "button"  # type: ignore[union-attr]
    assert step.target.name == "Current Bill"  # type: ignore[union-attr]
    assert step.target.locator and "getByRole" in step.target.locator  # type: ignore[union-attr]
    assert "e3" not in workflow.model_dump_json()


def test_compiler_refuses_brittle_ref_without_snapshot_evidence() -> None:
    with pytest.raises(WorkflowCompilationError, match="transient target"):
        compile_actions(
            name="invoice",
            description="",
            actions=[action(snapshot_before=None, durable_locator=None)],
        )

