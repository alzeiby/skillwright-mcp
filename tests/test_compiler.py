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
        "tool_name": "browser_click",
        "arguments": {"target": "e3", "element": "Current Bill"},
        "durable_locator": "getByRole('button', { name: 'Current Bill' })",
        "success": True,
        "snapshot_before": SNAPSHOT,
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


def test_compiler_reports_invalid_historical_click_metadata() -> None:
    with pytest.raises(WorkflowCompilationError, match="unsupported click button"):
        compile_actions(
            name="invoice",
            description="",
            actions=[
                action(
                    arguments={
                        "target": "e3",
                        "element": "Current Bill",
                        "button": "sideways",
                    }
                )
            ],
        )

