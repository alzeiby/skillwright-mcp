from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, quote_plus

import pytest

from skillwright_mcp.browser import BrowserController
from skillwright_mcp.db import Database
from skillwright_mcp.engine import WorkflowEngine
from skillwright_mcp.playwright import BrowserResult
from skillwright_mcp.secrets import resolve_secret, validate_secret_ref
from skillwright_mcp.skills import SkillService
from skillwright_mcp.workflow import ElementTarget, WorkflowDefinition

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
            structured: dict[str, Any] | None = None
        elif tool_name == "browser_type":
            secret = cast(str, args["text"])
            self.last_secret = secret
            encoded = quote(secret, safe="")
            encoded_plus = quote_plus(secret)
            text = f"typed={secret} encoded={encoded} encoded_plus={encoded_plus}"
            structured = {"echo": secret, encoded: "encoded-secret-used-as-a-response-key"}
        else:
            raise AssertionError(f"unexpected browser tool: {tool_name} {args}")
        return BrowserResult(
            ok=True,
            text=text,
            structured_content=structured,
        )

    async def close(self) -> None:
        self.close_calls += 1


def _secret_workflow(*, target_name: str = "Password") -> WorkflowDefinition:
    return WorkflowDefinition.model_validate(
        {
            "name": "secret-login",
            "inputs": {"password": {"type": "string", "secret": True}},
            "steps": [
                {
                    "op": "fill",
                    "target": {"role": "textbox", "name": target_name},
                    "value": "{{ password }}",
                }
            ],
        }
    )


async def _runtime(
    tmp_path: Path,
    database_name: str,
    *,
    target_name: str = "Password",
) -> tuple[Database, WorkflowEngine, SecretFixturePlaywright, str]:
    database = Database(tmp_path / database_name)
    await database.initialize()
    skill, _ = await database.create_skill_version(_secret_workflow(target_name=target_name))
    fake = SecretFixturePlaywright()
    return (
        database,
        WorkflowEngine(database, lambda: BrowserController(cast(Any, fake))),
        fake,
        skill.id,
    )


def _row_payload(row: Any) -> dict[str, Any]:
    return asdict(row)


def _sqlite_rows(path: Path, table: str) -> list[tuple[Any, ...]]:
    connection = sqlite3.connect(path)
    try:
        return list(connection.execute(f'SELECT * FROM "{table}"'))
    finally:
        connection.close()


def test_env_secret_resolution_re_reads_rotated_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SECRET_ENV_NAME, "first-secret")
    assert resolve_secret("TEST_PASSWORD") == "first-secret"

    monkeypatch.setenv(SECRET_ENV_NAME, "rotated-secret")
    assert resolve_secret("TEST_PASSWORD") == "rotated-secret"


def test_secret_references_are_environment_identifiers_only() -> None:
    assert validate_secret_ref("TEST_PASSWORD") == "TEST_PASSWORD"
    with pytest.raises(ValueError, match="secret reference must match"):
        validate_secret_ref("prod/password")


@pytest.mark.asyncio
async def test_recorded_secret_preserves_env_binding(tmp_path: Path) -> None:
    database = Database(tmp_path / "env-recording.db")
    await database.initialize()
    fake = SecretFixturePlaywright()
    browser = BrowserController(cast(Any, fake), database)
    skills = SkillService(database)
    try:
        assert (await skills.record_start(browser, "env-secret-recording"))["status"] == "recording"
        result = await browser.fill_secret(
            "password-field",
            SECRET_SENTINEL,
            secret_ref="TEST_PASSWORD",
            input_name="password",
            element="Password",
        )
        assert result.ok
        saved = await skills.record_stop(browser)
        assert saved["status"] == "saved"

        skill = await database.get_skill("env-secret-recording")
        assert skill is not None
        bindings = await database.skill_secret_bindings(skill.id)
        assert bindings == {"password": "TEST_PASSWORD"}
        status = await skills.secret_status("env-secret-recording")
        assert status["secrets"] == [{"input": "password", "configured": True}]
        assert "TEST_PASSWORD" not in json.dumps(status)
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_conflicting_recorded_secret_bindings_do_not_publish_partial_skill(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "secret-conflict.db")
    await database.initialize()
    fake = SecretFixturePlaywright()
    browser = BrowserController(cast(Any, fake), database)
    skills = SkillService(database)
    try:
        started = await skills.record_start(browser, "conflicting-secret")
        assert started == {"status": "recording", "name": "conflicting-secret"}
        assert (
            await browser.fill_secret(
                "password-field",
                "first-secret",
                secret_ref="FIRST_PASSWORD",
                input_name="password",
                element="Password",
            )
        ).ok
        assert (
            await browser.fill_secret(
                "password-field",
                "second-secret",
                secret_ref="SECOND_PASSWORD",
                input_name="password",
                element="Password",
            )
        ).ok

        stopped = await skills.record_stop(browser)

        assert stopped["status"] == "compile_failed"
        assert "multiple secret bindings" in stopped["error"]
        assert browser.active_recording is None
        assert await database.get_skill("conflicting-secret") is None
    finally:
        await database.close()


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
        )

        result = await engine.run_skill("secret-login")

        assert result["status"] == "succeeded"
        serialized_result = json.dumps(result, default=str, sort_keys=True)
        assert all(value not in serialized_result for value in forbidden)
        type_calls = [args for tool_name, args in fake.calls if tool_name == "browser_type"]
        assert type_calls == [
            {"target": "e1", "text": SECRET_SENTINEL, "element": "Password"}
        ]

        database_path = tmp_path / database_name
        persisted = json.dumps(
            {
                "workflow_versions": _sqlite_rows(database_path, "workflow_versions"),
                "browser_actions": _sqlite_rows(database_path, "browser_actions"),
                "secret_bindings": _sqlite_rows(database_path, "skill_secret_bindings"),
            },
            default=str,
            sort_keys=True,
        )
        assert all(value not in persisted for value in forbidden)
        assert _sqlite_rows(database_path, "browser_actions") == []
    finally:
        await database.close()

    database_bytes = (tmp_path / database_name).read_bytes()
    assert all(value.encode() not in database_bytes for value in forbidden)


@pytest.mark.asyncio
async def test_explicit_secret_fill_returns_only_redacted_echoes(tmp_path: Path) -> None:
    database = Database(tmp_path / "secret-fill.db")
    await database.initialize()
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
        returned = json.dumps(result.as_dict(), default=str, sort_keys=True)
        assert all(value not in returned for value in forbidden)
        assert "[REDACTED]" in returned
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_secret_failures_happen_before_browser_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(SECRET_ENV_NAME, raising=False)
    database, engine, fake, skill_id = await _runtime(tmp_path, "secret-missing.db")
    try:
        missing_binding = await engine.run_skill("secret-login")
        assert missing_binding["status"] == "secret_unavailable"
        assert missing_binding["reason"] == "secret_binding_missing"
        assert missing_binding["side_effect_state"] == "not_started"
        assert fake.calls == []

        await database.bind_skill_secret(
            skill_id=skill_id,
            input_name="password",
            secret_ref="TEST_PASSWORD",
        )
        missing_value = await engine.run_skill("secret-login")
        assert missing_value["status"] == "secret_unavailable"
        assert missing_value["reason"] == "secret_value_missing"
        assert missing_value["side_effect_state"] == "not_started"
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
        )
        result = await engine.run_skill(
            "secret-login",
            inputs={"password": "caller-controlled-value"},
        )
        assert result["status"] == "invalid_inputs"
        assert "server-bound and cannot be supplied" in result["error"]
        assert fake.calls == []
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_repair_re_resolves_rotated_secret_and_saves_only_validated_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = SECRET_SENTINEL
    rotated = "rotated! secret/%?#[]{}+value"
    monkeypatch.setenv(SECRET_ENV_NAME, original)
    database, engine, fake, skill_id = await _runtime(
        tmp_path,
        "secret-repair.db",
        target_name="Legacy Password",
    )
    try:
        await database.bind_skill_secret(
            skill_id=skill_id,
            input_name="password",
            secret_ref="TEST_PASSWORD",
        )
        broken = await engine.run_skill("secret-login")
        assert broken["status"] == "repair_required"
        assert broken["step"] == 0
        assert fake.calls == [("browser_snapshot", {})]

        monkeypatch.setenv(SECRET_ENV_NAME, rotated)
        repaired = await engine.repair_skill(
            "secret-login",
            base_version=1,
            step=0,
            replacement_target=ElementTarget(role="textbox", name="Password"),
        )

        assert repaired["status"] == "saved"
        assert repaired["version"] == 2
        type_calls = [args for tool_name, args in fake.calls if tool_name == "browser_type"]
        assert type_calls == [{"target": "e1", "text": rotated, "element": "Password"}]
        versions = await database.workflow_versions("secret-login")
        assert [row.version for row in versions] == [2, 1]

        persisted = json.dumps(
            [_row_payload(row) for row in versions],
            default=str,
            sort_keys=True,
        )
        forbidden = _forbidden_secret_variants(original) | _forbidden_secret_variants(rotated)
        assert all(value not in persisted for value in forbidden)
    finally:
        await database.close()
