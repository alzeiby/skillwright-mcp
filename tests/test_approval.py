from __future__ import annotations

from typing import Any, cast

import pytest
from sqlalchemy import select

from skillwright_mcp.browser import BrowserController
from skillwright_mcp.config import Settings
from skillwright_mcp.db import AuditEventRow, Database
from skillwright_mcp.engine import WorkflowEngine
from skillwright_mcp.playwright import BrowserResult
from skillwright_mcp.queue import RunDispatcher
from skillwright_mcp.workflow import WorkflowDefinition


class ApprovalFixturePlaywright:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def has_tool(self, _tool_name: str) -> bool:
        return False

    async def call(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> BrowserResult:
        self.calls.append(tool_name)
        if tool_name == "browser_snapshot":
            text = "\n".join(
                [
                    "- Page URL: http://approval.test/",
                    "- Page Title: Approval Fixture",
                    '- button "Publish now" [ref=e1]',
                ]
            )
        elif tool_name == "browser_click":
            text = "clicked"
        else:
            raise AssertionError(f"unexpected browser tool: {tool_name} {arguments}")
        return BrowserResult(
            tool_name=tool_name,
            ok=True,
            text=text,
            structured_content=None,
            raw={"text": text},
        )


async def _approval_runtime(tmp_path: Any) -> tuple[
    Database,
    WorkflowEngine,
    RunDispatcher,
    ApprovalFixturePlaywright,
    str,
]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'approval.db').as_posix()}")
    await database.initialize(create_schema=True)
    requester = await database.ensure_principal("requester@example.test", "developer")
    workflow = WorkflowDefinition.model_validate(
        {
            "name": "publish-draft",
            "steps": [
                {
                    "op": "click",
                    "target": {"role": "button", "name": "Publish now"},
                    "approval": {"reason": "Publishing is externally visible"},
                }
            ],
        }
    )
    await database.create_skill_version(workflow, owner_principal_id=requester.id)
    fake = ApprovalFixturePlaywright()
    browser = BrowserController(cast(Any, fake), database)
    engine = WorkflowEngine(database, browser)
    dispatcher = RunDispatcher(
        settings=Settings(
            database_url=database.url,
            execution_backend="inline",
        ),
        database=database,
        engine=engine,
    )
    return database, engine, dispatcher, fake, requester.id


@pytest.mark.asyncio
async def test_approval_gate_stops_before_mutation_and_resumes_once(tmp_path: Any) -> None:
    database, engine, dispatcher, fake, requester_id = await _approval_runtime(tmp_path)
    try:
        result = await engine.run_skill(
            "publish-draft",
            requested_by_principal_id=requester_id,
        )
        assert result["status"] == "approval_required"
        assert result["step"] == 0
        assert result["side_effect_state"] == "not_started"
        assert fake.calls == []

        approval_id = str(result["approval_id"])
        approved = await dispatcher.decide_approval(
            approval_id,
            approve=True,
            decided_by_principal_id=requester_id,
            comment="Reviewed",
        )
        assert approved["status"] == "succeeded"
        assert fake.calls == ["browser_snapshot", "browser_click"]

        stored = await database.get_approval(approval_id)
        assert stored is not None
        assert stored.status == "approved"
        assert stored.decided_by_principal_id == requester_id
        assert stored.comment == "Reviewed"
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_rejected_approval_never_executes_mutation(tmp_path: Any) -> None:
    database, engine, dispatcher, fake, requester_id = await _approval_runtime(tmp_path)
    try:
        result = await engine.run_skill(
            "publish-draft",
            idempotency_key="reject-me",
            requested_by_principal_id=requester_id,
        )
        assert result["status"] == "approval_required"
        rejected = await dispatcher.decide_approval(
            str(result["approval_id"]),
            approve=False,
            decided_by_principal_id=requester_id,
            comment="Do not publish",
        )
        assert rejected["status"] == "rejected"
        assert fake.calls == []

        run = await database.get_run(str(result["run_id"]))
        assert run is not None
        assert run.status == "rejected"
        assert run.finished_at is not None
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_cancelling_waiting_approval_closes_gate_and_blocks_late_decision(
    tmp_path: Any,
) -> None:
    database, engine, dispatcher, fake, requester_id = await _approval_runtime(tmp_path)
    try:
        pending = await engine.run_skill(
            "publish-draft",
            requested_by_principal_id=requester_id,
        )
        assert pending["status"] == "approval_required"
        approval_id = str(pending["approval_id"])

        cancelled = await dispatcher.cancel(
            str(pending["run_id"]),
            actor_principal_id=requester_id,
        )
        assert cancelled["status"] == "cancelled"
        assert cancelled["cancel_requested"] is True

        stored = await database.get_approval(approval_id)
        assert stored is not None
        assert stored.status == "cancelled"
        assert stored.decided_at is not None

        late = await dispatcher.decide_approval(
            approval_id,
            approve=True,
            decided_by_principal_id=requester_id,
        )
        assert late["status"] == "invalid_approval"
        assert fake.calls == []

        run = await database.get_run(str(pending["run_id"]))
        assert run is not None
        assert run.status == "cancelled"
        assert run.finished_at is not None

        async with database.sessions() as session:
            audit = await session.scalar(
                select(AuditEventRow).where(
                    AuditEventRow.event_type == "run.cancel_requested",
                    AuditEventRow.entity_id == run.id,
                )
            )
        assert audit is not None
        assert audit.principal_id == requester_id
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_cancelled_run_rejects_repeat_of_previously_approved_decision(tmp_path: Any) -> None:
    database, engine, dispatcher, fake, requester_id = await _approval_runtime(tmp_path)
    try:
        pending = await engine.run_skill(
            "publish-draft",
            requested_by_principal_id=requester_id,
        )
        approval_id = str(pending["approval_id"])

        # Model the Redis control-plane ordering: the decision is durable before the retained
        # worker resumes the browser session.
        stored = await database.decide_approval(
            approval_id,
            approve=True,
            decided_by_principal_id=requester_id,
        )
        assert stored.status == "approved"
        cancelled = await database.request_cancel(
            str(pending["run_id"]),
            actor_principal_id=requester_id,
        )
        assert cancelled is not None
        assert cancelled.status == "cancelled"

        repeated = await dispatcher.decide_approval(
            approval_id,
            approve=True,
            decided_by_principal_id=requester_id,
        )
        assert repeated["status"] == "invalid_approval"
        assert fake.calls == []

        run = await database.get_run(str(pending["run_id"]))
        assert run is not None
        assert run.status == "cancelled"
    finally:
        await database.close()
