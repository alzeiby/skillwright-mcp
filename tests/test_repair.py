from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from skillwright_mcp.browser import BrowserController
from skillwright_mcp.db import Database
from skillwright_mcp.engine import WorkflowEngine
from skillwright_mcp.playwright import BrowserResult
from skillwright_mcp.workflow import (
    ClickStep,
    ElementTarget,
    ExtractStep,
    NavigateStep,
    SkillCallStep,
    WaitStep,
    WorkflowDefinition,
    WorkflowInput,
    WorkflowOutput,
)


class RepairPlaywright:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> BrowserResult:
        args = dict(arguments or {})
        self.calls.append((tool_name, args))
        if tool_name == "browser_snapshot":
            text = (
                "- Page URL: https://example.test/form\n"
                "- Page Title: Form\n"
                '- button "Save" [ref=e1]\n'
                '- button "Replacement" [ref=e2]'
            )
            ok = True
        elif tool_name == "browser_click":
            text = "clicked"
            ok = True
        elif tool_name == "browser_wait_for":
            ok = "text" not in args
            text = "waited" if ok else "expected text was not found"
        else:
            raise AssertionError(f"unexpected tool: {tool_name}")
        return BrowserResult(
            ok=ok,
            text=text,
            structured_content=None,
        )

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_repair_evidence_reports_prior_successful_side_effects(tmp_path: Path) -> None:
    database = Database(tmp_path / "side-effects.db")
    await database.initialize()
    fake = RepairPlaywright()
    engine = WorkflowEngine(
        database,
        lambda: BrowserController(cast(Any, fake)),
    )
    try:
        await database.create_skill_version(
            WorkflowDefinition(
                name="two-clicks",
                steps=[
                    ClickStep(target=ElementTarget(role="button", name="Save")),
                    ClickStep(target=ElementTarget(role="button", name="Missing")),
                ],
            )
        )

        result = await engine.run_skill("two-clicks")

        assert result["status"] == "repair_required"
        assert result["step"] == 1
        assert result["side_effect_state"] == "completed"
        assert result["current_step_side_effect_state"] == "not_started"
        assert any(candidate["name"] == "Replacement" for candidate in result["candidates"])
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_agent_can_replace_non_target_step_and_only_validated_repair_is_saved(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "step-repair.db")
    await database.initialize()
    fake = RepairPlaywright()
    engine = WorkflowEngine(
        database,
        lambda: BrowserController(cast(Any, fake)),
    )
    try:
        await database.create_skill_version(
            WorkflowDefinition(
                name="wait-for-old-copy",
                steps=[WaitStep(text="Old site copy")],
            )
        )
        broken = await engine.run_skill("wait-for-old-copy")
        assert broken["status"] == "repair_required"
        assert broken["reason"] == "step_failed"
        assert broken["expected"] is None
        assert "expected text was not found" in broken["error"]

        repaired = await engine.repair_skill(
            "wait-for-old-copy",
            base_version=1,
            step=0,
            replacement_step=WaitStep(seconds=0),
        )

        assert repaired["status"] == "saved"
        assert repaired["validated"] is True
        assert repaired["version"] == 2
        versions = await database.workflow_versions("wait-for-old-copy")
        assert [row.version for row in versions] == [2, 1]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_full_step_repair_cannot_break_declared_output_type(tmp_path: Path) -> None:
    database = Database(tmp_path / "output-contract.db")
    await database.initialize()
    fake = RepairPlaywright()
    engine = WorkflowEngine(
        database,
        lambda: BrowserController(cast(Any, fake)),
    )
    try:
        await database.create_skill_version(
            WorkflowDefinition(
                name="typed-output",
                outputs={"count": WorkflowOutput(type="integer", source="count")},
                steps=[
                    ExtractStep(
                        target=ElementTarget(role="button", name="Save"),
                        save_as="count",
                        output_type="integer",
                    )
                ],
            )
        )

        repaired = await engine.repair_skill(
            "typed-output",
            base_version=1,
            step=0,
            replacement_step=ExtractStep(
                target=ElementTarget(role="button", name="Save"),
                save_as="count",
                output_type="string",
            ),
        )

        assert repaired["status"] == "repair_validation_failed"
        validation = repaired["validation"]
        assert validation["status"] == "failed"
        assert "expected 'integer'" in validation["error"]
        versions = await database.workflow_versions("typed-output")
        assert [row.version for row in versions] == [1]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_full_step_repair_template_error_is_structured_validation_failure(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "template-contract.db")
    await database.initialize()
    fake = RepairPlaywright()
    engine = WorkflowEngine(
        database,
        lambda: BrowserController(cast(Any, fake)),
    )
    try:
        await database.create_skill_version(
            WorkflowDefinition(
                name="template-repair",
                inputs={"destination": WorkflowInput(type="string")},
                steps=[NavigateStep(url="https://example.test/{{ destination }}")],
            )
        )

        repaired = await engine.repair_skill(
            "template-repair",
            base_version=1,
            step=0,
            replacement_step=NavigateStep(url="https://example.test/{{ missing }}"),
            inputs={"destination": "ok"},
        )

        assert repaired["status"] == "repair_validation_failed"
        validation = repaired["validation"]
        assert validation["status"] == "repair_required"
        assert validation["reason"] == "step_error"
        assert "unknown template variable" in validation["error"]
        versions = await database.workflow_versions("template-repair")
        assert [row.version for row in versions] == [1]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_agent_can_repair_multiple_broken_steps_with_one_candidate_workflow(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "multi-step-repair.db")
    await database.initialize()
    fake = RepairPlaywright()
    engine = WorkflowEngine(
        database,
        lambda: BrowserController(cast(Any, fake)),
    )
    try:
        await database.create_skill_version(
            WorkflowDefinition(
                name="two-broken-steps",
                steps=[WaitStep(text="Old first copy"), WaitStep(text="Old second copy")],
            )
        )

        broken = await engine.run_skill("two-broken-steps")
        assert broken["status"] == "repair_required"

        repaired = await engine.repair_skill(
            "two-broken-steps",
            base_version=1,
            replacement_workflow=WorkflowDefinition(
                name="two-broken-steps",
                steps=[WaitStep(seconds=0), WaitStep(seconds=0)],
            ),
        )

        assert repaired["status"] == "saved"
        assert repaired["validated"] is True
        assert repaired["repaired_step"] is None
        assert repaired["version"] == 2
        versions = await database.workflow_versions("two-broken-steps")
        assert [row.version for row in versions] == [2, 1]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_child_call_template_error_is_structured_during_repair_validation(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "child-template-repair.db")
    await database.initialize()
    fake = RepairPlaywright()
    engine = WorkflowEngine(
        database,
        lambda: BrowserController(cast(Any, fake)),
    )
    try:
        await database.create_skill_version(
            WorkflowDefinition(
                name="child",
                inputs={"destination": WorkflowInput(type="string")},
                steps=[NavigateStep(url="https://example.test/{{ destination }}")],
            )
        )
        await database.create_skill_version(
            WorkflowDefinition(
                name="parent",
                steps=[WaitStep(text="Old copy")],
            )
        )

        repaired = await engine.repair_skill(
            "parent",
            base_version=1,
            step=0,
            replacement_step=SkillCallStep(
                skill="child",
                version=1,
                inputs={"destination": "{{ missing }}"},
            ),
        )

        assert repaired["status"] == "repair_validation_failed"
        validation = repaired["validation"]
        assert validation["status"] == "repair_required"
        assert validation["reason"] == "step_error"
        assert "unknown template variable" in validation["error"]
        versions = await database.workflow_versions("parent")
        assert [row.version for row in versions] == [1]
    finally:
        await database.close()
