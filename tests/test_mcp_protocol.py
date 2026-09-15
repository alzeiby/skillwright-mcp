from __future__ import annotations

from pathlib import Path

import pytest
from mcp import Client

import skillwright_mcp.server as server_module
from skillwright_mcp.config import Settings


@pytest.mark.asyncio
async def test_mcp_server_exposes_browser_and_skill_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'protocol.db').as_posix()}",
        database_auto_create_schema=True,
        execution_backend="inline",
        playwright_output_dir=tmp_path / "playwright-output",
    )
    monkeypatch.setattr(server_module, "_server_settings", settings)

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
            "skill_secret_bind",
            "skill_secret_unbind",
            "skill_secret_status",
            "skill_approval_set",
            "skill_run",
            "skill_status",
            "skill_cancel",
            "skill_repair",
            "skill_approval_decide",
            "skill_versions",
            "skill_rollback",
        } <= names

        result = await client.call_tool("skill_list", {})
        assert not result.is_error
        assert result.structured_content == {"skills": []}
