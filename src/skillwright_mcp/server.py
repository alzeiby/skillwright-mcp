from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.subscriptions import InMemorySubscriptionBus
from mcp.shared.subscriptions import ToolsListChanged

from . import __version__
from .browser import BrowserController
from .config import load_settings
from .registry import SkillToolRegistry
from .runtime import Runtime, build_runtime
from .secrets import SecretResolutionError, resolve_secret, validate_secret_ref
from .skills import CompositionCall
from .workflow import (
    ElementTarget,
    ParameterBinding,
    WorkflowDefinition,
    WorkflowInput,
    WorkflowOutput,
    WorkflowStep,
    validate_public_input_name,
)

AppContext = Runtime
_subscriptions = InMemorySubscriptionBus()


@asynccontextmanager
async def app_lifespan(_: MCPServer[AppContext]) -> AsyncIterator[AppContext]:
    runtime = build_runtime(load_settings())
    await runtime.database.initialize()
    await _registry.refresh_all(runtime.database)
    try:
        yield runtime
    finally:
        await _registry.clear()
        await runtime.interactive_browser.close()


mcp = MCPServer(
    "Skillwright MCP",
    version=__version__,
    instructions=(
        "Skillwright records browser work performed through browser_* tools and turns successful "
        "interactions into persistent typed MCP tools. Use skill_record_start/stop or "
        "skill_save_from_history to create a skill. Saved skills appear as generated "
        "skillwright_* tools and execute deterministically. If one returns repair_required, "
        "inspect its structured page/candidate evidence (and browser_* tools if useful), then "
        "call skill_repair with an agent-selected replacement target, typed step, or complete "
        "workflow. Repairs are replay-validated before a new immutable version is saved. Use "
        "skill_compose to build higher-level tools from pinned saved-skill versions."
    ),
    lifespan=app_lifespan,
    subscriptions=_subscriptions,
)
_registry = SkillToolRegistry(mcp)


def _app(ctx: Context[AppContext]) -> AppContext:
    return ctx.request_context.lifespan_context


@asynccontextmanager
async def _browser_session(ctx: Context[AppContext]) -> AsyncIterator[BrowserController]:
    app = _app(ctx)
    async with app.interactive_lock:
        yield app.interactive_browser


async def _refresh_generated_tool(ctx: Context[AppContext], skill_name: str) -> None:
    await _registry.refresh_skill(_app(ctx).database, skill_name)
    await _subscriptions.publish(ToolsListChanged())


@mcp.tool()
async def browser_navigate(url: str, ctx: Context[AppContext]) -> dict[str, Any]:
    """Navigate the current interactive browser and record the action in local history."""

    async with _browser_session(ctx) as browser:
        return (await browser.navigate(url)).as_dict()


@mcp.tool()
async def browser_snapshot(
    ctx: Context[AppContext],
    target: str | None = None,
    depth: int | None = None,
) -> dict[str, Any]:
    """Capture the current Playwright accessibility snapshot."""

    async with _browser_session(ctx) as browser:
        return (await browser.snapshot(target=target, depth=depth)).as_dict()


@mcp.tool()
async def browser_click(
    target: str,
    ctx: Context[AppContext],
    element: str | None = None,
    double_click: bool = False,
    button: Literal["left", "right", "middle"] = "left",
) -> dict[str, Any]:
    """Click an element identified by a current Playwright snapshot reference."""

    async with _browser_session(ctx) as browser:
        return (
            await browser.click(
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
    """Fill an editable element through Microsoft's Playwright MCP."""

    async with _browser_session(ctx) as browser:
        return (await browser.fill(target, text, element=element, submit=submit)).as_dict()


@mcp.tool()
async def browser_fill_secret(
    target: str,
    secret_ref: str,
    input_name: str,
    ctx: Context[AppContext],
    element: str | None = None,
    submit: bool = False,
) -> dict[str, Any]:
    """Fill from an env-backed secret without returning or persisting its plaintext value."""

    try:
        validate_public_input_name(input_name)
        normalized_ref = validate_secret_ref(secret_ref)
        secret_value = resolve_secret(normalized_ref)
    except (ValueError, SecretResolutionError) as exc:
        return {"ok": False, "error": str(exc)}
    async with _browser_session(ctx) as browser:
        return (
            await browser.fill_secret(
                target,
                secret_value,
                secret_ref=normalized_ref,
                input_name=input_name,
                element=element,
                submit=submit,
            )
        ).as_dict()


@mcp.tool()
async def browser_select(
    target: str,
    values: list[str],
    ctx: Context[AppContext],
    element: str | None = None,
) -> dict[str, Any]:
    """Select one or more values in a dropdown through Playwright MCP."""

    async with _browser_session(ctx) as browser:
        return (await browser.select(target, values, element=element)).as_dict()


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
    async with _browser_session(ctx) as browser:
        return (await browser.wait(seconds=seconds, text=text, text_gone=text_gone)).as_dict()


@mcp.tool()
async def skill_record_start(
    name: str,
    ctx: Context[AppContext],
    description: str = "",
) -> dict[str, Any]:
    """Start recording browser_* actions as a named reusable Skillwright skill."""

    app = _app(ctx)
    async with _browser_session(ctx) as browser:
        return await app.skills.record_start(browser, name, description)


@mcp.tool()
async def skill_record_stop(ctx: Context[AppContext]) -> dict[str, Any]:
    """Stop recording, compile durable steps, save a version, and expose its generated MCP tool."""

    app = _app(ctx)
    async with _browser_session(ctx) as browser:
        result = await app.skills.record_stop(browser)
    if result.get("status") == "saved" and isinstance(result.get("skill"), str):
        await _refresh_generated_tool(ctx, str(result["skill"]))
    return result


@mcp.tool()
async def skill_save_from_history(
    name: str,
    start_event: int,
    end_event: int,
    ctx: Context[AppContext],
    description: str = "",
) -> dict[str, Any]:
    """Compile a contiguous successful browser-action range into a reusable generated MCP tool."""

    app = _app(ctx)
    async with _browser_session(ctx) as browser:
        result = await app.skills.save_from_history(
            name,
            start_event=start_event,
            end_event=end_event,
            history_scope=browser.history_scope,
            description=description,
        )
    if result.get("status") == "saved":
        await _refresh_generated_tool(ctx, name)
    return result


@mcp.tool()
async def skill_list(ctx: Context[AppContext]) -> dict[str, Any]:
    """List persisted skills, generated tool names, and current versions."""

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
    """Get one immutable persisted workflow definition."""

    return await _app(ctx).skills.get(name, version)


@mcp.tool()
async def skill_parameterize(
    name: str,
    bindings: list[ParameterBinding],
    ctx: Context[AppContext],
) -> dict[str, Any]:
    """Replace recorded literals with typed inputs and refresh the generated MCP tool schema."""

    result = await _app(ctx).skills.parameterize(name, bindings)
    if result.get("status") == "saved":
        await _refresh_generated_tool(ctx, name)
    return result


@mcp.tool()
async def skill_output_add(
    name: str,
    output_name: str,
    target: ElementTarget,
    ctx: Context[AppContext],
    output_type: Literal["string", "number", "integer", "boolean"] = "string",
    attribute: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Append a semantic extraction step and declare a typed output for a saved skill."""

    result = await _app(ctx).skills.add_output(
        name,
        output_name=output_name,
        target=target,
        output_type=output_type,
        attribute=attribute,
        description=description,
    )
    if result.get("status") == "saved":
        await _refresh_generated_tool(ctx, name)
    return result


@mcp.tool()
async def skill_secret_bind(
    name: str,
    input_name: str,
    secret_ref: str,
    ctx: Context[AppContext],
) -> dict[str, Any]:
    """Bind a secret skill input to an environment variable reference."""

    return await _app(ctx).skills.bind_secret(
        name,
        input_name=input_name,
        secret_ref=secret_ref,
    )


@mcp.tool()
async def skill_secret_unbind(
    name: str,
    input_name: str,
    ctx: Context[AppContext],
) -> dict[str, Any]:
    """Remove the environment binding for a secret workflow input."""

    return await _app(ctx).skills.unbind_secret(name, input_name=input_name)


@mcp.tool()
async def skill_secret_status(name: str, ctx: Context[AppContext]) -> dict[str, Any]:
    """Show which secret inputs are configured without exposing references or values."""

    return await _app(ctx).skills.secret_status(name)


@mcp.tool()
async def skill_repair(
    name: str,
    base_version: int,
    ctx: Context[AppContext],
    step: int | None = None,
    replacement_target: ElementTarget | None = None,
    replacement_step: WorkflowStep | None = None,
    replacement_workflow: WorkflowDefinition | None = None,
    inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Replay-validate an agent-proposed patch or candidate workflow before saving a new version."""

    result = await _app(ctx).engine.repair_skill(
        name,
        base_version=base_version,
        step=step,
        replacement_target=replacement_target,
        replacement_step=replacement_step,
        replacement_workflow=replacement_workflow,
        inputs=inputs,
    )
    if result.get("status") == "saved":
        await _refresh_generated_tool(ctx, name)
    return result


@mcp.tool()
async def skill_compose(
    name: str,
    calls: list[CompositionCall],
    ctx: Context[AppContext],
    description: str = "",
    inputs: dict[str, WorkflowInput] | None = None,
    outputs: dict[str, WorkflowOutput] | None = None,
) -> dict[str, Any]:
    """Create a higher-level generated tool from pinned versions of existing Skillwright skills."""

    result = await _app(ctx).skills.compose(
        name,
        calls,
        description=description,
        inputs=inputs,
        outputs=outputs,
    )
    if result.get("status") == "saved":
        await _refresh_generated_tool(ctx, name)
    return result


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
    """Create a new current version whose definition matches an older immutable version."""

    result = await _app(ctx).skills.rollback(name, version)
    if result.get("status") == "saved":
        await _refresh_generated_tool(ctx, name)
    return result
