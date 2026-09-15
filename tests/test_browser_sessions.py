from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from skillwright_mcp.browser import BrowserController
from skillwright_mcp.config import Settings
from skillwright_mcp.db import Database
from skillwright_mcp.playwright import BrowserResult
from skillwright_mcp.runtime import BrowserSessionPool, _build_interactive_browser
from skillwright_mcp.server import _browser_session_key, _transport_session_id


class StatefulPlaywright:
    def __init__(self) -> None:
        self.page_url = "about:blank"
        self.close_calls = 0

    async def has_tool(self, _tool_name: str) -> bool:
        return False

    async def call(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> BrowserResult:
        args = arguments or {}
        if tool_name == "browser_navigate":
            self.page_url = cast(str, args["url"])
            text = f"navigated to {self.page_url}"
        elif tool_name == "browser_snapshot":
            text = f"- Page URL: {self.page_url}"
        elif tool_name == "browser_type":
            text = f"typed {args.get('text', '')}"
        else:
            text = tool_name
        return BrowserResult(
            tool_name=tool_name,
            ok=True,
            text=text,
            structured_content=None,
            raw={"text": text},
        )

    async def close(self) -> None:
        self.close_calls += 1


class FakeTaskProtection:
    def __init__(self) -> None:
        self.protect_calls = 0
        self.unprotect_calls = 0
        self.protected = False

    async def protect(self) -> None:
        self.protect_calls += 1
        self.protected = True

    async def unprotect(self) -> None:
        if self.protected:
            self.unprotect_calls += 1
            self.protected = False


def _controller_factory(
    database: Database,
    playwrights: list[StatefulPlaywright],
) -> Any:
    def factory(_key: str) -> BrowserController:
        playwright = StatefulPlaywright()
        playwrights.append(playwright)
        return BrowserController(cast(Any, playwright), database)

    return factory


def test_interactive_browser_uses_hashed_session_output_directory(tmp_path: Path) -> None:
    settings = Settings(playwright_output_dir=tmp_path / "outputs")
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'artifact-path.db').as_posix()}")
    session_key = "principal:alice@example.test:session:super-secret-session-id"

    first = _build_interactive_browser(settings, database, session_key)
    second = _build_interactive_browser(
        settings,
        database,
        "principal:alice@example.test:session:another-session-id",
    )
    first_output = first.playwright._settings.playwright_output_dir
    second_output = second.playwright._settings.playwright_output_dir

    assert first_output.parent == settings.playwright_output_dir / "interactive-sessions"
    assert second_output.parent == settings.playwright_output_dir / "interactive-sessions"
    assert first_output != second_output
    assert "alice@example.test" not in str(first_output)
    assert "super-secret-session-id" not in str(first_output)
    assert len(first_output.name) == 64


def test_transport_session_key_separates_sessions_and_principals() -> None:
    principal_a = cast(Any, SimpleNamespace(id="principal-a"))
    principal_b = cast(Any, SimpleNamespace(id="principal-b"))
    direct_a = cast(Any, SimpleNamespace(session_id="session-a"))
    direct_b = cast(Any, SimpleNamespace(session_id="session-b"))

    assert _browser_session_key(direct_a, principal_a) != _browser_session_key(
        direct_b, principal_a
    )
    assert _browser_session_key(direct_a, principal_a) != _browser_session_key(
        direct_a, principal_b
    )

    compatibility_ctx = cast(
        Any,
        SimpleNamespace(
            session=SimpleNamespace(_connection=SimpleNamespace(session_id="sdk-session"))
        ),
    )
    assert _transport_session_id(compatibility_ctx) == "sdk-session"

    fallback_ctx = cast(
        Any,
        SimpleNamespace(session=SimpleNamespace(_connection=SimpleNamespace(session_id=None))),
    )
    assert _browser_session_key(fallback_ctx, principal_a) == "principal:principal-a:fallback"
    assert _browser_session_key(fallback_ctx, principal_b) == "principal:principal-b:fallback"


@pytest.mark.asyncio
async def test_browser_sessions_isolate_page_recording_and_secret_state(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'sessions.db').as_posix()}")
    await database.initialize(create_schema=True)
    playwrights: list[StatefulPlaywright] = []
    pool = BrowserSessionPool(_controller_factory(database, playwrights))
    secret = "session-a-secret-value"
    principal_id = "principal-a"
    session_a = "principal:principal-a:session:a"
    session_b = "principal:principal-a:session:b"
    session_c = "principal:principal-b:session:a"

    try:
        async with pool.use(session_a) as browser_a:
            await browser_a.navigate("https://session-a.test", actor_principal_id=principal_id)
            await browser_a.fill_secret(
                "password",
                secret,
                secret_ref="SESSION_A_PASSWORD",
                input_name="password",
                actor_principal_id=principal_id,
            )
            browser_a.set_active_recording(principal_id, "recording-a")
            assert browser_a._session_redactor.text(secret) == "[REDACTED]"

        async with pool.use(session_b) as browser_b:
            snapshot_b = await browser_b.snapshot(actor_principal_id=principal_id)
            assert browser_b is not browser_a
            assert snapshot_b.result is not None
            assert "about:blank" in snapshot_b.result.text
            assert "session-a.test" not in snapshot_b.result.text
            assert browser_b.active_recording_for(principal_id) is None
            assert browser_b._session_redactor.text(secret) == secret

        async with pool.use(session_c) as browser_c:
            assert browser_c is not browser_a
            assert browser_c is not browser_b
            assert browser_c._session_redactor.text(secret) == secret

        async with pool.use(session_a) as browser_a_again:
            assert browser_a_again is browser_a
            assert browser_a_again.active_recording_for(principal_id) == "recording-a"
            assert browser_a_again._session_redactor.text(secret) == "[REDACTED]"
    finally:
        await pool.close()
        await database.close()

    assert len(playwrights) == 3
    assert all(playwright.close_calls == 1 for playwright in playwrights)


@pytest.mark.asyncio
async def test_browser_session_pool_reaps_abandoned_sessions_and_clears_state(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'cleanup.db').as_posix()}")
    await database.initialize(create_schema=True)
    playwrights: list[StatefulPlaywright] = []
    now = [100.0]
    pool = BrowserSessionPool(
        _controller_factory(database, playwrights),
        idle_timeout_seconds=30.0,
        cleanup_interval_seconds=10.0,
        clock=lambda: now[0],
    )
    secret = "cleanup-secret"

    try:
        async with pool.use("principal:p:session:stale") as stale_browser:
            await stale_browser.fill_secret(
                "password",
                secret,
                secret_ref="PASSWORD",
                input_name="password",
                actor_principal_id="p",
            )
            stale_browser.set_active_recording("p", "recording-stale")

        now[0] = 131.0
        await pool.cleanup_idle()

        assert playwrights[0].close_calls == 1
        assert stale_browser._session_redactor.text(secret) == secret
        assert stale_browser.active_recording_for("p") is None
        assert stale_browser.latest_snapshot is None

        async with pool.use("principal:p:session:stale") as replacement:
            assert replacement is not stale_browser
    finally:
        await pool.close()
        await database.close()

    assert len(playwrights) == 2
    assert all(playwright.close_calls == 1 for playwright in playwrights)


@pytest.mark.asyncio
async def test_browser_session_pool_protects_ecs_task_until_last_session_is_reaped(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'protection.db').as_posix()}")
    await database.initialize(create_schema=True)
    playwrights: list[StatefulPlaywright] = []
    protection = FakeTaskProtection()
    now = [100.0]
    pool = BrowserSessionPool(
        _controller_factory(database, playwrights),
        idle_timeout_seconds=30.0,
        cleanup_interval_seconds=10.0,
        clock=lambda: now[0],
        task_protection=cast(Any, protection),
    )

    try:
        async with pool.use("principal:p:session:a"):
            pass
        async with pool.use("principal:p:session:b"):
            pass
        assert protection.protect_calls == 2
        assert protection.unprotect_calls == 0

        now[0] = 131.0
        await pool.cleanup_idle()
        assert protection.unprotect_calls == 1
    finally:
        await pool.close()
        await database.close()

    assert protection.unprotect_calls == 1


@pytest.mark.asyncio
async def test_same_session_calls_serialize_while_distinct_sessions_remain_parallel(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'concurrency.db').as_posix()}")
    await database.initialize(create_schema=True)
    playwrights: list[StatefulPlaywright] = []
    now = [100.0]
    pool = BrowserSessionPool(
        _controller_factory(database, playwrights),
        idle_timeout_seconds=30.0,
        cleanup_interval_seconds=10.0,
        clock=lambda: now[0],
    )
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()
    first_browser: list[BrowserController] = []
    second_browser: list[BrowserController] = []

    async def first_call() -> None:
        async with pool.use("principal:p:session:same") as browser:
            first_browser.append(browser)
            first_entered.set()
            await release_first.wait()

    async def second_call() -> None:
        await first_entered.wait()
        async with pool.use("principal:p:session:same") as browser:
            second_browser.append(browser)
            second_entered.set()

    first_task = asyncio.create_task(first_call())
    second_task = asyncio.create_task(second_call())
    try:
        await first_entered.wait()
        await asyncio.sleep(0)
        assert not second_entered.is_set()

        now[0] = 131.0
        await pool.cleanup_idle()
        assert playwrights[0].close_calls == 0

        async with pool.use("principal:p:session:other") as other_browser:
            assert other_browser is not first_browser[0]
            assert not second_entered.is_set()

        release_first.set()
        await asyncio.gather(first_task, second_task)
        assert second_entered.is_set()
        assert second_browser == first_browser
    finally:
        release_first.set()
        await asyncio.gather(first_task, second_task, return_exceptions=True)
        await pool.close()
        await database.close()
