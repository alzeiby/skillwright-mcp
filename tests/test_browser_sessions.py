from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from skillwright_mcp.browser import BrowserController
from skillwright_mcp.config import Settings
from skillwright_mcp.db import Database
from skillwright_mcp.engine import WorkflowEngine
from skillwright_mcp.playwright import BrowserResult
from skillwright_mcp.runtime import Runtime, build_runtime
from skillwright_mcp.server import _browser_session
from skillwright_mcp.skills import SkillService


class StatefulPlaywright:
    def __init__(self) -> None:
        self.page_url = "about:blank"
        self.close_calls = 0

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
            ok=True,
            text=text,
            structured_content=None,
        )

    async def close(self) -> None:
        self.close_calls += 1


def _runtime_with_fake_browser(
    tmp_path: Path,
) -> tuple[Runtime, StatefulPlaywright, BrowserController]:
    settings = Settings(
        data_dir=tmp_path,
        database_path=tmp_path / "runtime.db",
        playwright_output_dir=tmp_path / "outputs",
    )
    database = Database(settings.resolved_database_path())
    fake = StatefulPlaywright()
    browser = BrowserController(cast(Any, fake), database, history_scope="interactive-test")
    runtime = Runtime(
        database=database,
        engine=WorkflowEngine(database, lambda: browser),
        skills=SkillService(database),
        interactive_browser=browser,
    )
    return runtime, fake, browser


def test_each_stdio_runtime_gets_a_unique_interactive_scope_and_output_dir(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "shared")
    first = build_runtime(settings)
    second = build_runtime(settings)

    first_output = first.interactive_browser.playwright._settings.playwright_output_dir
    second_output = second.interactive_browser.playwright._settings.playwright_output_dir

    assert first_output is not None
    assert second_output is not None
    assert first_output.parent == settings.resolved_playwright_output_dir() / "interactive"
    assert second_output.parent == settings.resolved_playwright_output_dir() / "interactive"
    assert first_output != second_output
    assert first.interactive_browser.history_scope != second.interactive_browser.history_scope
    assert first.interactive_browser.history_scope.startswith("interactive-")
    assert second.interactive_browser.history_scope.startswith("interactive-")


@pytest.mark.asyncio
async def test_process_local_interactive_browser_retains_state_until_runtime_close(
    tmp_path: Path,
) -> None:
    runtime, fake, browser = _runtime_with_fake_browser(tmp_path)
    secret = "process-local-secret"
    await runtime.database.initialize()
    try:
        navigation = await browser.navigate("https://session.test")
        assert "tool" not in navigation.as_dict()
        await browser.fill_secret(
            "password",
            secret,
            secret_ref="PASSWORD",
            input_name="password",
        )
        snapshot = await browser.snapshot()
        browser.active_recording = ("local", "", 0)
        assert snapshot.result is not None
        assert "session.test" in snapshot.result.text
        assert browser.active_recording == ("local", "", 0)
        assert browser._session_redactor.text(secret) == "[REDACTED]"
    finally:
        await runtime.interactive_browser.close()

    assert fake.close_calls == 1
    assert browser.active_recording is None
    assert browser.latest_snapshot is None
    assert browser._session_redactor.text(secret) == secret


@pytest.mark.asyncio
async def test_interactive_mcp_operations_serialize_in_one_stdio_process(tmp_path: Path) -> None:
    runtime, _fake, browser = _runtime_with_fake_browser(tmp_path)
    ctx = cast(
        Any,
        SimpleNamespace(request_context=SimpleNamespace(lifespan_context=runtime)),
    )
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()
    seen: list[BrowserController] = []

    async def first_call() -> None:
        async with _browser_session(ctx) as current:
            seen.append(current)
            first_entered.set()
            await release_first.wait()

    async def second_call() -> None:
        await first_entered.wait()
        async with _browser_session(ctx) as current:
            seen.append(current)
            second_entered.set()

    first_task = asyncio.create_task(first_call())
    second_task = asyncio.create_task(second_call())
    try:
        await first_entered.wait()
        await asyncio.sleep(0)
        assert not second_entered.is_set()
        release_first.set()
        await asyncio.gather(first_task, second_task)
        assert second_entered.is_set()
        assert seen == [browser, browser]
    finally:
        release_first.set()
        await asyncio.gather(first_task, second_task, return_exceptions=True)
