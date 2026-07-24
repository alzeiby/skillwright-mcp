from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.shared.subscriptions import ToolsListChanged

import skillwright_mcp.server as server_module
from skillwright_mcp.config import Settings
from skillwright_mcp.db import Database
from skillwright_mcp.engine import WorkflowEngine
from skillwright_mcp.workflow import (
    NavigateStep,
    WorkflowDefinition,
    WorkflowInput,
    WorkflowOutput,
)


def _settings(tmp_path: Path, filename: str = "protocol.db") -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / filename,
        playwright_output_dir=tmp_path / "playwright-output",
    )


async def _seed_typed_skill(settings: Settings) -> str:
    database = Database(settings.resolved_database_path())
    await database.initialize()
    try:
        skill, _ = await database.create_skill_version(
            WorkflowDefinition(
                name="search invoices",
                description="Search invoices using persisted browser automation.",
                inputs={
                    "query": WorkflowInput(type="string", description="Invoice query"),
                    "count": WorkflowInput(type="integer", default=3),
                    "preview": WorkflowInput(type="boolean", required=False),
                    "threshold": WorkflowInput(type="number", default=1.5),
                    "password": WorkflowInput(type="string", secret=True),
                },
                outputs={
                    "model_dump": WorkflowOutput(
                        type="string",
                        description="Echo the query using an alias-safe output name.",
                        source="query",
                    )
                },
                steps=[NavigateStep(url="https://example.test/search?q={{ query }}")],
            )
        )
        return skill.tool_name
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_mcp_server_exposes_only_local_first_product_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server_module, "load_settings", lambda: _settings(tmp_path))

    async with Client(server_module.mcp) as client:
        listed = await client.list_tools()
        names = {tool.name for tool in listed.tools}

    assert {
        "browser_navigate",
        "browser_snapshot",
        "browser_click",
        "browser_fill",
        "browser_fill_secret",
        "browser_select",
        "browser_wait",
        "skill_record_start",
        "skill_record_stop",
        "skill_save_from_history",
        "skill_list",
        "skill_search",
        "skill_get",
        "skill_parameterize",
        "skill_secret_bind",
        "skill_secret_unbind",
        "skill_secret_status",
        "skill_repair",
        "skill_compose",
        "skill_versions",
        "skill_rollback",
    } <= names
    assert not {
        "skill_run",
        "skill_status",
        "skill_cancel",
        "skill_approval_set",
        "skill_approval_decide",
        "principal_set_role",
        "skill_access_grant",
    } & names


@pytest.mark.asyncio
async def test_persisted_skill_is_a_real_typed_mcp_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    generated_name = await _seed_typed_skill(settings)
    monkeypatch.setattr(server_module, "load_settings", lambda: settings)

    async with Client(server_module.mcp) as client:
        listed = await client.list_tools()
        generated = next(tool for tool in listed.tools if tool.name == generated_name)

    schema = generated.input_schema
    assert generated.title == "search invoices"
    assert schema["properties"]["query"]["type"] == "string"
    assert schema["properties"]["count"]["type"] == "integer"
    assert schema["properties"]["count"]["default"] == 3
    preview_schema = schema["properties"]["preview"]
    assert {entry["type"] for entry in preview_schema["anyOf"]} == {"boolean", "null"}
    assert schema["properties"]["threshold"]["type"] == "number"
    assert schema["properties"]["threshold"]["default"] == 1.5
    assert schema["required"] == ["query"]
    assert "password" not in schema["properties"]
    assert "ctx" not in schema["properties"]
    dumped = generated.model_dump(mode="json", by_alias=True)
    assert dumped["_meta"]["skillwright"] == {
        "skill": "search invoices",
        "version": 1,
        "generated": True,
    }
    output_schema = generated.output_schema
    assert output_schema is not None
    output_union = output_schema["properties"]["outputs"]["anyOf"]
    output_ref = next(entry["$ref"] for entry in output_union if "$ref" in entry)
    output_definition = output_schema["$defs"][output_ref.rsplit("/", 1)[-1]]
    assert output_definition["properties"]["model_dump"]["type"] == "string"
    assert output_definition["required"] == ["model_dump"]


@pytest.mark.asyncio
async def test_generated_tool_injects_context_applies_defaults_and_validates_strictly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    generated_name = await _seed_typed_skill(settings)
    monkeypatch.setattr(server_module, "load_settings", lambda: settings)
    calls: list[tuple[str, dict[str, Any] | None]] = []

    async def fake_run_skill(
        self: WorkflowEngine,
        name: str,
        *,
        inputs: dict[str, Any] | None = None,
        version: int | None = None,
    ) -> dict[str, Any]:
        assert self.database.path == settings.resolved_database_path()
        assert version == 1
        calls.append((name, inputs))
        return {"status": "succeeded", "workflow": name, "inputs": inputs or {}}

    monkeypatch.setattr(WorkflowEngine, "run_skill", fake_run_skill)
    async with Client(server_module.mcp) as client:
        external = Database(settings.resolved_database_path())
        await external.initialize()
        try:
            stored = await external.get_workflow_version("search invoices")
            assert stored is not None
            workflow = WorkflowDefinition.model_validate(stored[1].definition)
            await external.create_skill_version(
                workflow,
                parent_version=1,
                expected_current_version=1,
            )
        finally:
            await external.close()
        result = await client.call_tool(generated_name, {"query": "Q4"})
        invalid = await client.call_tool(generated_name, {"query": "Q4", "count": "7"})

    assert not result.is_error
    assert result.structured_content == {
        "status": "succeeded",
        "workflow": "search invoices",
        "inputs": {"query": "Q4", "count": 3, "preview": None, "threshold": 1.5},
        "outputs": None,
    }
    assert calls == [
        (
            "search invoices",
            {"query": "Q4", "count": 3, "preview": None, "threshold": 1.5},
        )
    ]
    assert invalid.is_error


@pytest.mark.asyncio
async def test_skill_schema_change_publishes_standard_tools_list_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    generated_name = await _seed_typed_skill(settings)
    monkeypatch.setattr(server_module, "load_settings", lambda: settings)

    async with Client(server_module.mcp) as client:
        async with client.listen(tools_list_changed=True) as subscription:
            assert subscription.honored.tools_list_changed is True
            mutation = await client.call_tool(
                "skill_parameterize",
                {
                    "name": "search invoices",
                    "bindings": [
                        {
                            "step": 0,
                            "field": "url",
                            "input_name": "destination",
                            "input_type": "string",
                            "description": "Full destination URL",
                        }
                    ],
                },
            )
            assert not mutation.is_error
            assert mutation.structured_content is not None
            assert mutation.structured_content["status"] == "saved"
            async with asyncio.timeout(2):
                event = await anext(subscription)
            assert isinstance(event, ToolsListChanged)

        listed = await client.list_tools()
        generated = next(tool for tool in listed.tools if tool.name == generated_name)

    assert generated.input_schema["properties"]["destination"]["type"] == "string"
    assert set(generated.input_schema["required"]) == {"query", "destination"}
    dumped = generated.model_dump(mode="json", by_alias=True)
    assert dumped["_meta"]["skillwright"]["version"] == 2
