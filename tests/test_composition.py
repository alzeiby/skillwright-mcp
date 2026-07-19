from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from skillwright_mcp.browser import BrowserController
from skillwright_mcp.db import Database
from skillwright_mcp.engine import WorkflowEngine
from skillwright_mcp.playwright import BrowserResult
from skillwright_mcp.skills import CompositionCall, SkillService
from skillwright_mcp.workflow import (
    ExtractStep,
    NavigateStep,
    SkillCallStep,
    WorkflowDefinition,
    WorkflowInput,
    WorkflowOutput,
)


class CompositionPlaywright:
    def __init__(self) -> None:
        self.url = "about:blank"
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> BrowserResult:
        args = dict(arguments or {})
        self.calls.append((tool_name, args))
        if tool_name == "browser_navigate":
            self.url = str(args["url"])
            text = f"navigated {self.url}"
        elif tool_name == "browser_snapshot":
            title = self.url.rsplit("/", 1)[-1]
            text = (
                f"- Page URL: {self.url}\n"
                "- Page Title: Report\n"
                f'- heading "Report {title}" [ref=e1]'
            )
        else:
            raise AssertionError(f"unexpected browser tool: {tool_name}")
        return BrowserResult(
            ok=True,
            text=text,
            structured_content=None,
        )

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_composition_pins_child_versions_and_preserves_typed_outputs(tmp_path: Path) -> None:
    database = Database(tmp_path / "composition.db")
    await database.initialize()
    service = SkillService(database)
    fake = CompositionPlaywright()
    engine = WorkflowEngine(
        database,
        lambda: BrowserController(cast(Any, fake)),
    )
    try:
        await database.create_skill_version(
            WorkflowDefinition(
                name="login",
                inputs={"user": WorkflowInput(type="string")},
                steps=[NavigateStep(url="https://example.test/login/{{ user }}")],
            )
        )
        _, report_v1 = await database.create_skill_version(
            WorkflowDefinition(
                name="report",
                inputs={"report_id": WorkflowInput(type="integer")},
                outputs={"title": WorkflowOutput(type="string", source="title")},
                steps=[
                    NavigateStep(url="https://example.test/reports/{{ report_id }}"),
                    ExtractStep(
                        target={"role": "heading", "name": "Report 42"},
                        save_as="title",
                    ),
                ],
            )
        )

        composed = await service.compose(
            "download_report",
            [
                CompositionCall(skill="login", inputs={"user": "{{ user }}"}),
                CompositionCall(
                    skill="report",
                    inputs={"report_id": "{{ report_id }}"},
                    outputs={"title": "report_title"},
                ),
            ],
            inputs={
                "user": WorkflowInput(type="string"),
                "report_id": WorkflowInput(type="integer"),
            },
        )
        assert composed["status"] == "saved"
        parent = WorkflowDefinition.model_validate(composed["workflow"])
        assert isinstance(parent.steps[1], SkillCallStep)
        assert parent.steps[1].version == report_v1.version == 1
        assert parent.outputs["report_title"].type == "string"

        # Advancing the child must not silently change the already-saved parent composition.
        await database.create_skill_version(
            WorkflowDefinition(
                name="report",
                inputs={"report_id": WorkflowInput(type="integer")},
                steps=[NavigateStep(url="https://example.test/v2/{{ report_id }}")],
            ),
            expected_current_version=1,
        )

        result = await engine.run_skill(
            "download_report",
            inputs={"user": "alice", "report_id": 42},
        )

        assert result["status"] == "succeeded"
        assert result["outputs"] == {"report_title": "Report 42"}
        navigations = [args["url"] for tool, args in fake.calls if tool == "browser_navigate"]
        assert navigations == [
            "https://example.test/login/alice",
            "https://example.test/reports/42",
        ]
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_composition_validates_nullability_and_pins_older_same_name_versions(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "composition-validation.db")
    await database.initialize()
    service = SkillService(database)
    try:
        await database.create_skill_version(
            WorkflowDefinition(
                name="leaf",
                inputs={"count": WorkflowInput(type="integer")},
                steps=[NavigateStep(url="https://example.test/{{ count }}")],
            )
        )
        mismatch = await service.compose(
            "bad_types",
            [CompositionCall(skill="leaf", inputs={"count": "{{ count }}"})],
            inputs={"count": WorkflowInput(type="string")},
        )
        assert mismatch["status"] == "invalid_composition"
        assert "expects 'integer'" in mismatch["error"]

        await database.create_skill_version(
            WorkflowDefinition(
                name="string_leaf",
                inputs={"label": WorkflowInput(type="string")},
                steps=[NavigateStep(url="https://example.test/{{ label }}")],
            )
        )
        interpolated = await service.compose(
            "interpolated_string",
            [
                CompositionCall(
                    skill="string_leaf",
                    inputs={"label": "{{ count }} items {{ count }}"},
                )
            ],
            inputs={"count": WorkflowInput(type="integer")},
        )
        assert interpolated["status"] == "saved"

        nullable = await service.compose(
            "nullable_parent",
            [CompositionCall(skill="leaf", inputs={"count": "{{ count }}"})],
            inputs={"count": WorkflowInput(type="integer", required=False)},
        )
        assert nullable["status"] == "invalid_composition"
        assert "may be null" in nullable["error"]

        nullable_output = await service.compose(
            "nullable_output",
            [CompositionCall(skill="leaf", inputs={"count": 1})],
            inputs={"maybe": WorkflowInput(type="string", required=False)},
            outputs={"out": WorkflowOutput(type="string", source="maybe")},
        )
        assert nullable_output["status"] == "invalid_composition"
        assert "nullable value" in nullable_output["error"]

        await database.create_skill_version(
            WorkflowDefinition(
                name="numeric_leaf",
                inputs={"value": WorkflowInput(type="number")},
                steps=[NavigateStep(url="https://example.test/{{ value }}")],
            )
        )
        widening = await service.compose(
            "integer_to_number",
            [CompositionCall(skill="numeric_leaf", inputs={"value": "{{ value }}"})],
            inputs={"value": WorkflowInput(type="integer")},
        )
        assert widening["status"] == "saved"

        first = await service.compose(
            "first",
            [CompositionCall(skill="leaf", inputs={"count": 1})],
        )
        assert first["status"] == "saved"
        second = await service.compose("second", [CompositionCall(skill="first")])
        assert second["status"] == "saved"
        newer_first = await service.compose("first", [CompositionCall(skill="second")])
        assert newer_first["status"] == "saved"
        workflow = WorkflowDefinition.model_validate(newer_first["workflow"])
        assert isinstance(workflow.steps[0], SkillCallStep)
        assert workflow.steps[0].skill == "second"
        assert workflow.steps[0].version == 1

        fake = CompositionPlaywright()
        engine = WorkflowEngine(
            database,
            lambda: BrowserController(cast(Any, fake)),
        )
        replay = await engine.run_skill("first")
        assert replay["status"] == "succeeded"
        assert [args["url"] for tool, args in fake.calls if tool == "browser_navigate"] == [
            "https://example.test/1"
        ]
    finally:
        await database.close()
