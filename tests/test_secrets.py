from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, quote_plus

import pytest
from sqlalchemy import select

from skillwright_mcp.browser import BrowserController
from skillwright_mcp.db import (
    ApprovalRow,
    AuditEventRow,
    BrowserActionRow,
    Database,
    RepairRow,
    RunRow,
    SkillSecretBindingRow,
    StepExecutionRow,
    WorkflowVersionRow,
)
from skillwright_mcp.engine import WorkflowEngine
from skillwright_mcp.playwright import BrowserResult
from skillwright_mcp.workflow import WorkflowDefinition

SECRET_ENV_NAME = "SKILLWRIGHT_SECRET_TEST_PASSWORD"
SECRET_SENTINEL = "p@ss word!#$%&'()*+,/:;=?[]{}\\\"<>|~"
_PERCENT_ESCAPE_RE = re.compile(r"%[0-9A-Fa-f]{2}")


def _lower_percent_escapes(value: str) -> str:
    return _PERCENT_ESCAPE_RE.sub(lambda match: match.group(0).lower(), value)


def _forbidden_secret_variants(value: str) -> set[str]:
    encoded = quote(value, safe="")
    encoded_plus = quote_plus(value)
    return {
        value,
        encoded,
        encoded_plus,
        _lower_percent_escapes(encoded),
        _lower_percent_escapes(encoded_plus),
    }


class SecretFixturePlaywright:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.last_secret: str | None = None
        self.close_calls = 0

    async def has_tool(self, _tool_name: str) -> bool:
        return False

    async def call(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> BrowserResult:
        args = dict(arguments or {})
        self.calls.append((tool_name, args))
        if tool_name == "browser_snapshot":
            lines = [
                "- Page URL: https://secret.test/login",
                "- Page Title: Secret fixture",
                '- textbox "Password" [ref=e1]',
            ]
            if self.last_secret is not None:
                lines.append(f'- text "echo={self.last_secret}"')
            text = "\n".join(lines)
            raw = {"text": text}
            structured: dict[str, Any] | None = None
        elif tool_name == "browser_type":
            secret = cast(str, args["text"])
            self.last_secret = secret
            encoded = quote(secret, safe="")
            encoded_plus = quote_plus(secret)
            encoded_lower = _lower_percent_escapes(encoded)
            encoded_plus_lower = _lower_percent_escapes(encoded_plus)
            text = (
                f"typed={secret} encoded={encoded} encoded_plus={encoded_plus} "
                f"encoded_lower={encoded_lower} encoded_plus_lower={encoded_plus_lower}"
            )
            raw = {
                "text": text,
                "echo": secret,
                "encoded": encoded,
                "encoded_plus": encoded_plus,
                "encoded_lower": encoded_lower,
                "encoded_plus_lower": encoded_plus_lower,
                secret: "secret-used-as-a-response-key",
            }
            structured = {
                "echo": secret,
                "encoded": encoded,
                "encoded_plus": encoded_plus,
                "encoded_lower": encoded_lower,
                "encoded_plus_lower": encoded_plus_lower,
                encoded: "encoded-secret-used-as-a-response-key",
                encoded_lower: "lowercase-encoded-secret-used-as-a-response-key",
            }
        else:
            raise AssertionError(f"unexpected browser tool: {tool_name} {args}")
        return BrowserResult(
            tool_name=tool_name,
            ok=True,
            text=text,
            structured_content=structured,
            raw=raw,
        )

    async def close(self) -> None:
        self.close_calls += 1


def _secret_workflow() -> WorkflowDefinition:
    return WorkflowDefinition.model_validate(
        {
            "name": "secret-login",
            "inputs": {"password": {"type": "string", "secret": True}},
            "steps": [
                {
                    "op": "fill",
                    "target": {"role": "textbox", "name": "Password"},
                    "value": "{{ password }}",
                }
            ],
        }
    )


async def _runtime(tmp_path: Path, database_name: str) -> tuple[
    Database,
    WorkflowEngine,
    SecretFixturePlaywright,
    str,
]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / database_name).as_posix()}")
    await database.initialize(create_schema=True)
    skill, _ = await database.create_skill_version(_secret_workflow())
    fake = SecretFixturePlaywright()
    browser = BrowserController(cast(Any, fake), database)
    return database, WorkflowEngine(database, browser), fake, skill.id


def _row_payload(row: Any) -> dict[str, Any]:
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


@pytest.mark.asyncio
async def test_bound_secret_reaches_playwright_but_never_persists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SECRET_ENV_NAME, SECRET_SENTINEL)
    database_name = "secret-success.db"
    database, engine, fake, skill_id = await _runtime(tmp_path, database_name)
    forbidden = _forbidden_secret_variants(SECRET_SENTINEL)
    try:
        await database.bind_skill_secret(
            skill_id=skill_id,
            input_name="password",
            secret_ref="TEST_PASSWORD",
            updated_by_principal_id=None,
        )

        result = await engine.run_skill("secret-login")

        assert result["status"] == "succeeded"
        serialized_result = json.dumps(result, default=str, sort_keys=True)
        assert all(value not in serialized_result for value in forbidden)
        type_calls = [args for tool_name, args in fake.calls if tool_name == "browser_type"]
        assert len(type_calls) == 1
        assert type_calls[0]["text"] == SECRET_SENTINEL

        async with database.sessions() as session:
            versions = (
                await session.scalars(
                    select(WorkflowVersionRow).where(WorkflowVersionRow.skill_id == skill_id)
                )
            ).all()
            runs = (
                await session.scalars(select(RunRow).where(RunRow.skill_id == skill_id))
            ).all()
            run_ids = [row.id for row in runs]
            actions = (
                await session.scalars(
                    select(BrowserActionRow).where(BrowserActionRow.run_id.in_(run_ids))
                )
            ).all()
            executions = (
                await session.scalars(
                    select(StepExecutionRow).where(StepExecutionRow.run_id.in_(run_ids))
                )
            ).all()
            repairs = (await session.scalars(select(RepairRow))).all()
            approvals = (await session.scalars(select(ApprovalRow))).all()
            bindings = (
                await session.scalars(
                    select(SkillSecretBindingRow).where(SkillSecretBindingRow.skill_id == skill_id)
                )
            ).all()
            audits = (await session.scalars(select(AuditEventRow))).all()

        persisted = json.dumps(
            {
                "workflow_versions": [_row_payload(row) for row in versions],
                "runs": [_row_payload(row) for row in runs],
                "browser_actions": [_row_payload(row) for row in actions],
                "step_executions": [_row_payload(row) for row in executions],
                "repairs": [_row_payload(row) for row in repairs],
                "approvals": [_row_payload(row) for row in approvals],
                "secret_bindings": [_row_payload(row) for row in bindings],
                "audit_events": [_row_payload(row) for row in audits],
            },
            default=str,
            sort_keys=True,
        )
        assert all(value not in persisted for value in forbidden)
        assert "[REDACTED]" in persisted
    finally:
        await database.close()

    database_bytes = (tmp_path / database_name).read_bytes()
    assert all(value.encode() not in database_bytes for value in forbidden)


@pytest.mark.asyncio
async def test_explicit_secret_fill_returns_only_redacted_echoes(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'secret-fill.db').as_posix()}")
    await database.initialize(create_schema=True)
    fake = SecretFixturePlaywright()
    browser = BrowserController(cast(Any, fake), database)
    forbidden = _forbidden_secret_variants(SECRET_SENTINEL)
    try:
        result = await browser.fill_secret(
            "e1",
            SECRET_SENTINEL,
            secret_ref="TEST_PASSWORD",
            input_name="password",
            element="Password",
        )

        assert result.ok
        type_calls = [args for tool_name, args in fake.calls if tool_name == "browser_type"]
        assert type_calls == [
            {
                "target": "e1",
                "text": SECRET_SENTINEL,
                "element": "Password",
            }
        ]
        returned = json.dumps(result.as_dict(), default=str, sort_keys=True)
        assert all(value not in returned for value in forbidden)
        assert "[REDACTED]" in returned
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_session_redactor_survives_later_page_then_releases_on_browser_close(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'secret-lifecycle.db').as_posix()}")
    await database.initialize(create_schema=True)
    fake = SecretFixturePlaywright()
    browser = BrowserController(cast(Any, fake), database)
    try:
        await browser.fill_secret(
            "e1",
            SECRET_SENTINEL,
            secret_ref="TEST_PASSWORD",
            input_name="password",
        )
        later_page = await browser.snapshot()

        assert later_page.result is not None
        assert SECRET_SENTINEL not in later_page.result.text
        assert "[REDACTED]" in later_page.result.text

        await browser.close()

        assert fake.close_calls == 1
        assert browser.latest_snapshot is None
        assert browser._session_redactor.text(SECRET_SENTINEL) == SECRET_SENTINEL
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_secret_run_rejects_missing_binding(tmp_path: Path) -> None:
    database, engine, fake, _skill_id = await _runtime(tmp_path, "secret-missing-binding.db")
    try:
        result = await engine.run_skill("secret-login")

        assert result == {
            "status": "secret_unavailable",
            "workflow": "secret-login",
            "input": "password",
            "reason": "secret_binding_missing",
        }
        assert fake.calls == []
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_secret_run_fails_before_browser_when_environment_value_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(SECRET_ENV_NAME, raising=False)
    database, engine, fake, skill_id = await _runtime(tmp_path, "secret-missing-env.db")
    try:
        await database.bind_skill_secret(
            skill_id=skill_id,
            input_name="password",
            secret_ref="TEST_PASSWORD",
            updated_by_principal_id=None,
        )

        result = await engine.run_skill("secret-login")

        assert result["status"] == "failed"
        assert result["reason"] == "secret_unavailable"
        assert result["side_effect_state"] == "not_started"
        assert fake.calls == []
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_caller_cannot_supply_server_bound_secret(tmp_path: Path) -> None:
    database, engine, fake, skill_id = await _runtime(tmp_path, "secret-caller-input.db")
    try:
        await database.bind_skill_secret(
            skill_id=skill_id,
            input_name="password",
            secret_ref="TEST_PASSWORD",
            updated_by_principal_id=None,
        )

        result = await engine.run_skill(
            "secret-login",
            inputs={"password": "caller-controlled-value"},
        )

        assert result["status"] == "invalid_inputs"
        assert "server-bound and cannot be supplied by the caller" in result["error"]
        assert fake.calls == []
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_secret_is_re_resolved_and_redacted_after_approval_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SECRET_ENV_NAME, SECRET_SENTINEL)
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'secret-approval.db').as_posix()}")
    await database.initialize(create_schema=True)
    principal = await database.ensure_principal("secret-approver@example.test", "developer")
    workflow = WorkflowDefinition.model_validate(
        {
            "name": "secret-approval",
            "inputs": {"password": {"type": "string", "secret": True}},
            "steps": [
                {
                    "op": "fill",
                    "target": {"role": "textbox", "name": "Password"},
                    "value": "{{ password }}",
                    "approval": {"reason": "Credential use requires approval"},
                }
            ],
        }
    )
    skill, _ = await database.create_skill_version(workflow, owner_principal_id=principal.id)
    await database.bind_skill_secret(
        skill_id=skill.id,
        input_name="password",
        secret_ref="TEST_PASSWORD",
        updated_by_principal_id=principal.id,
    )
    fake = SecretFixturePlaywright()
    browser = BrowserController(cast(Any, fake), database)
    engine = WorkflowEngine(database, browser)
    try:
        pending = await engine.run_skill(
            workflow.name,
            requested_by_principal_id=principal.id,
        )
        assert pending["status"] == "approval_required"
        assert pending["side_effect_state"] == "not_started"
        assert fake.calls == []

        await database.decide_approval(
            str(pending["approval_id"]),
            approve=True,
            decided_by_principal_id=principal.id,
            comment="approved",
        )
        resumed = await engine.resume_waiting_run(str(pending["run_id"]))

        assert resumed["status"] == "succeeded"
        type_calls = [args for tool_name, args in fake.calls if tool_name == "browser_type"]
        assert len(type_calls) == 1
        assert type_calls[0]["text"] == SECRET_SENTINEL
        forbidden = _forbidden_secret_variants(SECRET_SENTINEL)
        async with database.sessions() as session:
            actions = (
                await session.scalars(
                    select(BrowserActionRow).where(BrowserActionRow.run_id == pending["run_id"])
                )
            ).all()
            executions = (
                await session.scalars(
                    select(StepExecutionRow).where(StepExecutionRow.run_id == pending["run_id"])
                )
            ).all()
            approvals = (
                await session.scalars(
                    select(ApprovalRow).where(ApprovalRow.run_id == pending["run_id"])
                )
            ).all()
        persisted = json.dumps(
            {
                "actions": [_row_payload(row) for row in actions],
                "executions": [_row_payload(row) for row in executions],
                "approvals": [_row_payload(row) for row in approvals],
            },
            default=str,
            sort_keys=True,
        )
        assert all(value not in persisted for value in forbidden)
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_secret_is_re_resolved_and_redacted_after_repair_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_secret = SECRET_SENTINEL
    rotated_secret = "rotated! secret/%?#[]{}+value"
    monkeypatch.setenv(SECRET_ENV_NAME, original_secret)
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'secret-repair.db').as_posix()}")
    await database.initialize(create_schema=True)
    workflow = WorkflowDefinition.model_validate(
        {
            "name": "secret-repair",
            "inputs": {"password": {"type": "string", "secret": True}},
            "steps": [
                {
                    "op": "fill",
                    "target": {"role": "textbox", "name": "Legacy Password"},
                    "value": "{{ password }}",
                }
            ],
        }
    )
    skill, _ = await database.create_skill_version(workflow)
    await database.bind_skill_secret(
        skill_id=skill.id,
        input_name="password",
        secret_ref="TEST_PASSWORD",
        updated_by_principal_id=None,
    )
    fake = SecretFixturePlaywright()
    browser = BrowserController(cast(Any, fake), database)
    engine = WorkflowEngine(database, browser)
    repair_actor = await database.ensure_principal("repair-selector@example.test", "developer")
    await database.grant_skill_permission(skill.id, repair_actor.id, "edit")
    try:
        broken = await engine.run_skill(workflow.name)
        assert broken["status"] == "repair_required"
        assert broken["session_available"] is True
        replacement = next(
            candidate for candidate in broken["candidates"] if candidate["name"] == "Password"
        )
        assert [tool for tool, _args in fake.calls] == ["browser_snapshot"]

        # Repair must resolve the binding again instead of retaining the value seen on the
        # failed attempt. This also exercises redaction after repair validation snapshots.
        monkeypatch.setenv(SECRET_ENV_NAME, rotated_secret)
        repaired = await engine.repair(
            str(broken["run_id"]),
            step=int(broken["step"]),
            replacement_element_id=str(replacement["id"]),
            persist=True,
            actor_principal_id=repair_actor.id,
        )

        assert repaired["status"] == "succeeded"
        assert repaired["saved_workflow_version"] == 2
        type_calls = [args for tool, args in fake.calls if tool == "browser_type"]
        assert len(type_calls) == 1
        assert type_calls[0]["text"] == rotated_secret

        forbidden = _forbidden_secret_variants(original_secret) | _forbidden_secret_variants(
            rotated_secret
        )
        async with database.sessions() as session:
            run = await session.scalar(select(RunRow).where(RunRow.id == broken["run_id"]))
            actions = (
                await session.scalars(
                    select(BrowserActionRow).where(BrowserActionRow.run_id == broken["run_id"])
                )
            ).all()
            executions = (
                await session.scalars(
                    select(StepExecutionRow).where(StepExecutionRow.run_id == broken["run_id"])
                )
            ).all()
            repairs = (
                await session.scalars(select(RepairRow).where(RepairRow.run_id == broken["run_id"]))
            ).all()
            repair_applied_audit = await session.scalar(
                select(AuditEventRow).where(
                    AuditEventRow.event_type == "repair.applied",
                    AuditEventRow.entity_id == repairs[0].id,
                )
            )
            versions = (
                await session.scalars(
                    select(WorkflowVersionRow).where(WorkflowVersionRow.skill_id == skill.id)
                )
            ).all()
        assert run is not None
        assert len(repairs) == 1
        assert repairs[0].requested_by_principal_id == repair_actor.id
        assert repair_applied_audit is not None
        assert repair_applied_audit.principal_id == repair_actor.id
        persisted = json.dumps(
            {
                "run": _row_payload(run),
                "actions": [_row_payload(row) for row in actions],
                "executions": [_row_payload(row) for row in executions],
                "repairs": [_row_payload(row) for row in repairs],
                "versions": [_row_payload(row) for row in versions],
            },
            default=str,
            sort_keys=True,
        )
        assert all(value not in persisted for value in forbidden)
        assert "[REDACTED]" in persisted
    finally:
        await database.close()
