from __future__ import annotations

from pathlib import Path

import pytest

from skillwright_mcp.db import Database
from skillwright_mcp.skills import SkillService
from skillwright_mcp.workflow import (
    ElementTarget,
    FillStep,
    NavigateStep,
    ParameterBinding,
    SelectStep,
    WorkflowDefinition,
)


@pytest.mark.asyncio
async def test_parameterization_creates_typed_new_version(tmp_path: Path) -> None:
    database = Database(tmp_path / "parameterize.db")
    await database.initialize()
    try:
        target = ElementTarget(role="textbox", name="Account number")
        workflow = WorkflowDefinition(
            name="invoice",
            steps=[
                FillStep(target=target, value="ACC-42"),
                SelectStep(
                    target=ElementTarget(role="combobox", name="Statement month"),
                    values=["september"],
                ),
            ],
        )
        await database.create_skill_version(workflow)
        service = SkillService(database)

        result = await service.parameterize(
            "invoice",
            [
                ParameterBinding(step=0, field="value", input_name="account"),
                ParameterBinding(
                    step=1,
                    field="select_value",
                    item_index=0,
                    input_name="month",
                ),
            ],
        )

        assert result["status"] == "saved"
        assert result["version"] == 2
        saved = WorkflowDefinition.model_validate(result["workflow"])
        assert set(saved.inputs) == {"account", "month"}
        assert saved.steps[0].value == "{{ account }}"  # type: ignore[union-attr]
        assert saved.steps[1].values == ["{{ month }}"]  # type: ignore[union-attr]
        assert saved.prepare_inputs({"account": "ACC-99", "month": "august"}) == {
            "account": "ACC-99",
            "month": "august",
        }
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_parameterization_rejects_reserved_generated_tool_input_names(tmp_path: Path) -> None:
    database = Database(tmp_path / "reserved-input.db")
    await database.initialize()
    try:
        await database.create_skill_version(
            WorkflowDefinition(
                name="invoice",
                steps=[NavigateStep(url="https://example.test/invoice")],
            )
        )
        service = SkillService(database)

        result = await service.parameterize(
            "invoice",
            [ParameterBinding(step=0, field="url", input_name="ctx")],
        )

        assert result["status"] == "invalid_binding"
        assert "reserved" in result["error"] or "callable MCP parameter names" in result["error"]
        versions = await database.workflow_versions("invoice")
        assert [row.version for row in versions] == [1]
    finally:
        await database.close()
