from __future__ import annotations

import pytest
from pydantic import ValidationError

from skillwright_mcp.workflow import (
    ElementTarget,
    NavigateStep,
    WorkflowDefinition,
    WorkflowInput,
    render_template,
)


def test_workflow_models_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ElementTarget(role="button", name="Save", mystery="value")  # type: ignore[call-arg]


def test_workflow_input_validation_is_typed_and_strict() -> None:
    workflow = WorkflowDefinition(
        name="example",
        inputs={
            "account": WorkflowInput(type="string"),
            "month": WorkflowInput(type="integer", default=9),
        },
        steps=[NavigateStep(url="https://example.test/{{ account }}")],
    )

    assert workflow.prepare_inputs({"account": "A-1"}) == {"account": "A-1", "month": 9}
    with pytest.raises(ValueError, match="must be a string"):
        workflow.prepare_inputs({"account": 12})
    with pytest.raises(ValueError, match="unknown workflow inputs"):
        workflow.prepare_inputs({"account": "A-1", "extra": True})


def test_template_renderer_only_substitutes_named_values() -> None:
    assert render_template("invoice/{{ account }}", {"account": "123"}) == "invoice/123"
    with pytest.raises(ValueError, match="unknown template variable"):
        render_template("{{ missing }}", {})


@pytest.mark.parametrize("input_name", ["ctx", "class", "_query"])
def test_workflow_rejects_input_names_that_cannot_be_generated_mcp_parameters(
    input_name: str,
) -> None:
    with pytest.raises(ValidationError, match="callable MCP parameter names"):
        WorkflowDefinition(
            name="bad-input",
            inputs={input_name: WorkflowInput()},
            steps=[NavigateStep(url="https://example.test")],
        )

