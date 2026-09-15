from __future__ import annotations

from pathlib import Path

import pytest

from skillwright_mcp.db import Database
from skillwright_mcp.workflow import NavigateStep, WorkflowDefinition


def sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


@pytest.mark.asyncio
async def test_versions_are_append_only_and_stale_base_is_rejected(tmp_path: Path) -> None:
    database = Database(sqlite_url(tmp_path / "db.sqlite"))
    await database.initialize()
    try:
        workflow = WorkflowDefinition(
            name="invoice",
            steps=[NavigateStep(url="https://example.test")],
        )
        skill, first = await database.create_skill_version(workflow)
        assert skill.current_version == 1
        _, second = await database.create_skill_version(
            workflow,
            parent_version=1,
            expected_current_version=1,
        )
        assert second.version == 2
        with pytest.raises(ValueError, match="stale workflow base"):
            await database.create_skill_version(
                workflow,
                parent_version=1,
                expected_current_version=1,
            )
        versions = await database.workflow_versions("invoice")
        assert [row.version for row in versions] == [2, 1]
        assert first.id != second.id
    finally:
        await database.close()

