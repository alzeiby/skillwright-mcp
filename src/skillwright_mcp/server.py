from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver import Context

from . import __version__
from .browser import BrowserController
from .config import Settings
from .db import Database
from .engine import WorkflowEngine
from .playwright import PlaywrightMCPClient
from .queue import RunDispatcher
from .skills import SkillService
from .workflow import ParameterBinding


@dataclass(slots=True)
class AppContext:
    settings: Settings
    database: Database
    playwright: PlaywrightMCPClient
    browser: BrowserController
    engine: WorkflowEngine
    skills: SkillService
    dispatcher: RunDispatcher


@asynccontextmanager
async def app_lifespan(_: MCPServer[AppContext]) -> AsyncIterator[AppContext]:
    settings = Settings()
    database = Database(settings.database_url)
    await database.initialize(create_schema=settings.database_auto_create_schema)
    playwright = PlaywrightMCPClient(settings)
    browser = BrowserController(playwright, database)
    engine = WorkflowEngine(database, browser)
    skills = SkillService(database, browser, engine)
    dispatcher = RunDispatcher(settings=settings, database=database, engine=engine)
    await dispatcher.start()
    context = AppContext(
        settings=settings,
        database=database,
        playwright=playwright,
        browser=browser,
        engine=engine,
        skills=skills,
        dispatcher=dispatcher,
    )
    try:
        yield context
    finally:
        await dispatcher.close()
        await playwright.close()
        await database.close()


mcp = MCPServer(
    "Skillwright MCP",
    version=__version__,
    instructions=(
        "Use browser_* tools to perform browser work through Microsoft's Playwright MCP. "
        "Use skill_record_start/stop to save a successful interaction, then skill_run for "
        "deterministic replay. If a run returns repair_required, inspect its candidates and "
        "call skill_repair with the selected replacement element id."
    ),
    lifespan=app_lifespan,
)


def _app(ctx: Context[AppContext]) -> AppContext:
    return ctx.request_context.lifespan_context


@mcp.tool()
async def browser_navigate(url: str, ctx: Context[AppContext]) -> dict[str, Any]:
    """Navigate the current browser to a URL and record the action in Skillwright history."""

    return (await _app(ctx).browser.navigate(url)).as_dict()


@mcp.tool()
async def browser_snapshot(
    ctx: Context[AppContext],
    target: str | None = None,
    depth: int | None = None,
) -> dict[str, Any]:
    """Capture the current Playwright accessibility snapshot."""

    return (await _app(ctx).browser.snapshot(target=target, depth=depth)).as_dict()


@mcp.tool()
async def browser_click(
    target: str,
    ctx: Context[AppContext],
    element: str | None = None,
    double_click: bool = False,
    button: Literal["left", "right", "middle"] = "left",
) -> dict[str, Any]:
    """Click an element identified by a current Playwright snapshot target."""

    return (
        await _app(ctx).browser.click(
            target,
            element=element,
            double_click=double_click,
            button=button,
        )
    ).as_dict()


@mcp.tool()
async def browser_fill(
    target: str,
    text: str,
    ctx: Context[AppContext],
    element: str | None = None,
    submit: bool = False,
) -> dict[str, Any]:
    """Fill an editable element through Playwright MCP."""

    return (await _app(ctx).browser.fill(target, text, element=element, submit=submit)).as_dict()


@mcp.tool()
async def browser_select(
    target: str,
    values: list[str],
    ctx: Context[AppContext],
    element: str | None = None,
) -> dict[str, Any]:
    """Select one or more values in a dropdown through Playwright MCP."""

    return (await _app(ctx).browser.select(target, values, element=element)).as_dict()


@mcp.tool()
async def browser_wait(
    ctx: Context[AppContext],
    seconds: float | None = None,
    text: str | None = None,
    text_gone: str | None = None,
) -> dict[str, Any]:
    """Wait for time or text conditions through Playwright MCP."""

    if seconds is None and text is None and text_gone is None:
        return {"ok": False, "error": "provide seconds, text, or text_gone"}
    return (await _app(ctx).browser.wait(seconds=seconds, text=text, text_gone=text_gone)).as_dict()


@mcp.tool()
async def skill_record_start(
    name: str,
    ctx: Context[AppContext],
    description: str = "",
) -> dict[str, Any]:
    """Start recording subsequent proxied browser actions as a named reusable skill."""

    return await _app(ctx).skills.record_start(name, description)


@mcp.tool()
async def skill_record_stop(ctx: Context[AppContext]) -> dict[str, Any]:
    """Stop the active recording, compile it, validate it, and save a new skill version."""

    return await _app(ctx).skills.record_stop()


@mcp.tool()
async def skill_save_from_history(
    name: str,
    start_event: int,
    end_event: int,
    ctx: Context[AppContext],
    description: str = "",
) -> dict[str, Any]:
    """Compile a prior contiguous browser-action range into a reusable skill."""

    return await _app(ctx).skills.save_from_history(
        name,
        start_event=start_event,
        end_event=end_event,
        description=description,
    )


@mcp.tool()
async def skill_list(ctx: Context[AppContext]) -> dict[str, Any]:
    """List persisted skills and their current versions."""

    return await _app(ctx).skills.list()


@mcp.tool()
async def skill_search(
    query: str,
    ctx: Context[AppContext],
    limit: int = 10,
) -> dict[str, Any]:
    """Search persisted skills by name and description."""

    return await _app(ctx).skills.search(query, limit)


@mcp.tool()
async def skill_get(
    name: str,
    ctx: Context[AppContext],
    version: int | None = None,
) -> dict[str, Any]:
    """Get one persisted workflow definition."""

    return await _app(ctx).skills.get(name, version)


@mcp.tool()
async def skill_parameterize(
    name: str,
    bindings: list[ParameterBinding],
    ctx: Context[AppContext],
) -> dict[str, Any]:
    """Replace recorded literals with typed workflow inputs and save a new version."""

    return await _app(ctx).skills.parameterize(name, bindings)


@mcp.tool()
async def skill_run(
    name: str,
    ctx: Context[AppContext],
    inputs: dict[str, Any] | None = None,
    version: int | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Queue a deterministic browser skill run (or execute inline in local development)."""

    return await _app(ctx).dispatcher.submit(
        name,
        inputs=inputs,
        version=version,
        idempotency_key=idempotency_key,
    )


@mcp.tool()
async def skill_status(run_id: str, ctx: Context[AppContext]) -> dict[str, Any]:
    """Inspect persisted status and failure context for a workflow run."""

    return await _app(ctx).skills.status(run_id)


@mcp.tool()
async def skill_cancel(run_id: str, ctx: Context[AppContext]) -> dict[str, Any]:
    """Request cancellation of a queued or running skill execution."""

    return await _app(ctx).dispatcher.cancel(run_id)


@mcp.tool()
async def skill_repair(
    run_id: str,
    step: int,
    replacement_element_id: str,
    ctx: Context[AppContext],
    persist: bool = True,
) -> dict[str, Any]:
    """Apply one candidate target repair, continue the run, and optionally save a new version."""

    return await _app(ctx).dispatcher.repair(
        run_id,
        step=step,
        replacement_element_id=replacement_element_id,
        persist=persist,
    )


@mcp.tool()
async def skill_versions(name: str, ctx: Context[AppContext]) -> dict[str, Any]:
    """List immutable versions of a persisted skill."""

    return await _app(ctx).skills.versions(name)


@mcp.tool()
async def skill_rollback(
    name: str,
    version: int,
    ctx: Context[AppContext],
) -> dict[str, Any]:
    """Create a new current version whose definition matches an older skill version."""

    return await _app(ctx).skills.rollback(name, version)
