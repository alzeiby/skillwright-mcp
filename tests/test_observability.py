from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

import skillwright_mcp.browser as browser_module
import skillwright_mcp.engine as engine_module
from skillwright_mcp.browser import BrowserController
from skillwright_mcp.db import Database
from skillwright_mcp.engine import WorkflowEngine
from skillwright_mcp.secrets import Redactor
from skillwright_mcp.workflow import WorkflowDefinition


class _Span:
    def set_attribute(self, _name: str, _value: Any) -> None:
        return None


class _SpanContext:
    def __enter__(self) -> _Span:
        return _Span()

    def __exit__(self, *_args: object) -> None:
        return None


class _CapturingTracer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def start_as_current_span(self, name: str, **kwargs: Any) -> _SpanContext:
        self.calls.append((name, kwargs))
        return _SpanContext()


class _NeverPlaywright:
    async def call(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("the patched step executor should fail first")

    async def has_tool(self, _name: str) -> bool:
        return False


class _FailingPlaywright:
    async def call(self, *_args: object, **_kwargs: object) -> None:
        raise RuntimeError("SECRET_SENTINEL_MUST_NOT_REACH_OTEL")

    async def has_tool(self, _name: str) -> bool:
        return False


@pytest.mark.asyncio
async def test_execution_spans_disable_automatic_exception_recording(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'otel.db').as_posix()}")
    await database.initialize(create_schema=True)
    workflow = WorkflowDefinition.model_validate(
        {
            "name": "otel-secret-safety",
            "steps": [{"op": "navigate", "url": "https://example.test"}],
        }
    )
    skill, version = await database.create_skill_version(workflow)
    run = await database.create_run(
        skill=skill,
        version=version,
        inputs={},
        status="running",
    )
    browser = BrowserController(cast(Any, _NeverPlaywright()), database)
    engine = WorkflowEngine(database, browser)
    tracer = _CapturingTracer()

    async def fail_step(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise RuntimeError("SECRET_SENTINEL_MUST_NOT_REACH_OTEL")

    monkeypatch.setattr(engine_module, "tracer", lambda: tracer)
    monkeypatch.setattr(engine, "_execute_step", fail_step)

    try:
        with pytest.raises(RuntimeError, match="SECRET_SENTINEL"):
            await engine._run_execution_segment(
                workflow=workflow,
                skill=skill,
                version_row=version,
                run=run,
                start_index=0,
                variables={},
                attempt=1,
            )

        assert [name for name, _ in tracer.calls] == ["skill.run", "workflow.step"]
        for _name, kwargs in tracer.calls:
            assert kwargs["record_exception"] is False
            assert kwargs["set_status_on_exception"] is False
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_browser_span_disables_automatic_exception_recording(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'otel-browser.db').as_posix()}")
    await database.initialize(create_schema=True)
    browser = BrowserController(cast(Any, _FailingPlaywright()), database)
    tracer = _CapturingTracer()
    monkeypatch.setattr(browser_module, "tracer", lambda: tracer)

    try:
        result = await browser.navigate(
            "https://example.test",
            redactor=Redactor.from_values(["SECRET_SENTINEL_MUST_NOT_REACH_OTEL"]),
        )

        assert not result.ok
        assert result.error == "RuntimeError: [REDACTED]"
        assert [name for name, _ in tracer.calls] == ["browser.action"]
        kwargs = tracer.calls[0][1]
        assert kwargs["record_exception"] is False
        assert kwargs["set_status_on_exception"] is False
    finally:
        await database.close()
