from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from mcp import Client
from mcp.server import MCPServer
from mcp.server.auth.settings import AuthSettings

import skillwright_mcp.server as server_module
from skillwright_mcp.auth import BearerTokenAuthenticator, MCPBearerTokenVerifier
from skillwright_mcp.config import Settings
from skillwright_mcp.db import Database
from skillwright_mcp.playwright import PlaywrightMCPClient
from skillwright_mcp.runtime import Runtime


class StubReadinessRuntime:
    def __init__(
        self,
        checks: dict[str, str],
        *,
        start_error: Exception | None = None,
    ) -> None:
        self.checks = checks
        self.start_error = start_error
        self.calls = 0
        self.start_calls = 0
        self.settings = Settings(healthcheck_timeout_seconds=0.05)

    async def readiness(self) -> dict[str, str]:
        self.calls += 1
        return self.checks

    async def start(self) -> None:
        self.start_calls += 1
        if self.start_error is not None:
            raise self.start_error


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("checks", "expected_status"),
    [
        ({"database": "ok", "schema": "ok", "redis": "ok"}, 200),
        ({"database": "unavailable", "schema": "ok", "redis": "ok"}, 503),
        ({"database": "ok", "schema": "outdated", "redis": "ok"}, 503),
        ({"database": "ok", "schema": "ok", "redis": "unavailable"}, 503),
    ],
)
async def test_streamable_http_readiness_requires_all_reported_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    checks: dict[str, str],
    expected_status: int,
) -> None:
    runtime = StubReadinessRuntime(checks)
    monkeypatch.setattr(server_module, "_http_runtime", cast(Runtime, runtime))
    app = server_module.mcp.streamable_http_app(host="127.0.0.1")
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))

    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        response = await client.get("/health/ready")

    assert response.status_code == expected_status
    assert response.json() == {
        "status": "ready" if expected_status == 200 else "not_ready"
    }
    assert runtime.calls == 1
    assert runtime.start_calls == (1 if expected_status == 200 else 0)


@pytest.mark.asyncio
async def test_streamable_http_readiness_route_bypasses_bearer_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = StubReadinessRuntime({"database": "ok"})
    monkeypatch.setattr(server_module, "_http_runtime", cast(Runtime, runtime))

    token = "configured-service-token"
    verifier = MCPBearerTokenVerifier(
        BearerTokenAuthenticator({hashlib.sha256(token.encode()).hexdigest(): "service"}),
        issuer="https://auth.example.test",
        resource="http://127.0.0.1/mcp",
    )
    auth_server: MCPServer[Any] = MCPServer(
        "readiness-auth-test",
        auth=AuthSettings.model_validate(
            {
                "issuer_url": "https://auth.example.test",
                "resource_server_url": "http://127.0.0.1/mcp",
                "validate_token_resource": True,
            }
        ),
        token_verifier=verifier,
    )
    auth_server.custom_route("/health/ready", methods=["GET"])(server_module.health_ready)
    app = auth_server.streamable_http_app(host="127.0.0.1")
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))

    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        protected = await client.get("/mcp")
        ready = await client.get("/health/ready")

    assert protected.status_code == 401
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}
    assert runtime.calls == 1
    assert runtime.start_calls == 1


@pytest.mark.asyncio
async def test_streamable_http_readiness_requires_full_runtime_start_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = StubReadinessRuntime(
        {"database": "ok", "schema": "ok", "redis": "ok"},
        start_error=RuntimeError("startup incomplete"),
    )
    monkeypatch.setattr(server_module, "_http_runtime", cast(Runtime, runtime))
    app = server_module.mcp.streamable_http_app(host="127.0.0.1")
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))

    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}
    assert runtime.calls == 1
    assert runtime.start_calls == 1


@pytest.mark.asyncio
async def test_streamable_http_readiness_survives_dependency_startup_failure_without_playwright(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'readiness-recovery.db').as_posix()}",
        database_auto_create_schema=True,
        execution_backend="inline",
        allow_unauthenticated_local=False,
        bootstrap_admin_principal="bootstrap@example.test",
        healthcheck_timeout_seconds=1.0,
    )
    monkeypatch.setattr(server_module, "_server_settings", settings)

    original_initialize = Database.initialize
    initialize_calls = 0

    async def fail_initial_database_start(
        self: Database,
        *,
        create_schema: bool = True,
    ) -> None:
        nonlocal initialize_calls
        initialize_calls += 1
        if initialize_calls == 1:
            raise OSError("database temporarily unavailable")
        await original_initialize(self, create_schema=create_schema)

    monkeypatch.setattr(Database, "initialize", fail_initial_database_start)

    async def fail_if_playwright_connects(_self: PlaywrightMCPClient) -> None:
        raise AssertionError("readiness must not start Playwright")

    monkeypatch.setattr(PlaywrightMCPClient, "connect", fail_if_playwright_connects)
    app = server_module.mcp.streamable_http_app(host="127.0.0.1")
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client,
    ):
        ready = await client.get("/health/ready")
        ready_again = await client.get("/health/ready")
        runtime = server_module._http_runtime
        assert runtime is not None
        bootstrap_admin = await runtime.database.get_principal_by_external_key(
            "bootstrap@example.test"
        )

    assert initialize_calls == 2
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}
    assert ready_again.status_code == 200
    assert ready_again.json() == {"status": "ready"}
    assert bootstrap_admin is not None
    assert bootstrap_admin.role == "admin"
