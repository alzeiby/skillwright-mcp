from __future__ import annotations

from pathlib import Path

import pytest
from mcp import Client

from skillwright_mcp.server import mcp


@pytest.mark.asyncio
async def test_mcp_server_exposes_browser_and_skill_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "SKILLWRIGHT_DATABASE_URL",
        f"sqlite+aiosqlite:///{(tmp_path / 'protocol.db').as_posix()}",
    )
    monkeypatch.setenv("SKILLWRIGHT_DATABASE_AUTO_CREATE_SCHEMA", "true")
    monkeypatch.setenv("SKILLWRIGHT_EXECUTION_BACKEND", "inline")
    monkeypatch.setenv("SKILLWRIGHT_PLAYWRIGHT_OUTPUT_DIR", str(tmp_path / "playwright-output"))

    async with Client(mcp) as client:
        listed = await client.list_tools()
        names = {tool.name for tool in listed.tools}
        assert {
            "browser_navigate",
            "browser_snapshot",
            "browser_click",
            "browser_fill",
            "browser_select",
            "browser_wait",
            "skill_record_start",
            "skill_record_stop",
            "skill_save_from_history",
            "skill_list",
            "skill_search",
            "skill_get",
            "skill_run",
            "skill_status",
            "skill_cancel",
            "skill_repair",
            "skill_versions",
            "skill_rollback",
        } <= names

        result = await client.call_tool("skill_list", {})
        assert not result.is_error
        assert result.structured_content == {"skills": []}
