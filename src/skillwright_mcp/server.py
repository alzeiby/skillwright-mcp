from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal, cast

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Context, authenticated_principal
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__
from .auth import (
    AuthorizationError,
    BearerTokenAuthenticator,
    MCPBearerTokenVerifier,
    Role,
    SkillPermission,
)
from .browser import BrowserController
from .config import Settings
from .db import PrincipalRow, RunRow, SkillRow
from .runtime import Runtime, build_runtime
from .secrets import SecretProvider, SecretResolutionError, validate_secret_ref
from .skills import SkillService
from .workflow import ParameterBinding

AppContext = Runtime
logger = logging.getLogger(__name__)
_server_settings = Settings()
_http_runtime: Runtime | None = None
_bearer_auth = BearerTokenAuthenticator(_server_settings.auth_token_hashes)
_token_verifier = (
    MCPBearerTokenVerifier(
        _bearer_auth,
        issuer=_server_settings.auth_issuer_url,
        resource=_server_settings.mcp_resource_server_url,
    )
    if _bearer_auth.configured
    else None
)
_auth_settings = (
    AuthSettings.model_validate(
        {
            "issuer_url": _server_settings.auth_issuer_url,
            "resource_server_url": _server_settings.mcp_resource_server_url,
            "validate_token_resource": _server_settings.mcp_resource_server_url is not None,
        }
    )
    if _token_verifier is not None
    else None
)


@asynccontextmanager
async def app_lifespan(_: MCPServer[AppContext]) -> AsyncIterator[AppContext]:
    global _http_runtime

    runtime = build_runtime(_server_settings)
    previous_runtime = _http_runtime
    _http_runtime = runtime
    try:
        try:
            async with asyncio.timeout(_server_settings.healthcheck_timeout_seconds):
                await runtime.start()
        except Exception as exc:
            logger.warning(
                "MCP server started before dependencies were ready (%s)",
                type(exc).__name__,
            )
        yield runtime
    finally:
        _http_runtime = previous_runtime
        await runtime.close()


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
    auth=_auth_settings,
    token_verifier=_token_verifier,
)


async def health_ready(_: Request) -> JSONResponse:
    runtime = _http_runtime
    if runtime is None:
        return JSONResponse(status_code=503, content={"status": "not_ready"})

    try:
        checks = await runtime.readiness()
    except Exception:
        return JSONResponse(status_code=503, content={"status": "not_ready"})

    if not checks or any(value != "ok" for value in checks.values()):
        return JSONResponse(status_code=503, content={"status": "not_ready"})

    try:
        async with asyncio.timeout(runtime.settings.healthcheck_timeout_seconds):
            await runtime.start()
    except Exception:
        return JSONResponse(status_code=503, content={"status": "not_ready"})

    return JSONResponse(status_code=200, content={"status": "ready"})


mcp.custom_route("/health/ready", methods=["GET"])(health_ready)


def _app(ctx: Context[AppContext]) -> AppContext:
    return ctx.request_context.lifespan_context


def _transport_session_id(ctx: Context[AppContext]) -> str | None:
    """Return the server-issued MCP transport session id when this SDK exposes one."""

    session_id = getattr(ctx, "session_id", None)
    if isinstance(session_id, str) and session_id:
        return session_id

    # MCPServer's compatibility Context does not yet forward Context.session_id from the
    # newer server API. Its ServerSession still owns the same SDK Connection, whose
    # server-issued id is populated for stateful Streamable HTTP and absent on stdio/stateless.
    session = cast(Any, ctx.session)
    connection = getattr(session, "_connection", None)
    session_id = getattr(connection, "session_id", None)
    return session_id if isinstance(session_id, str) and session_id else None


def _browser_session_key(ctx: Context[AppContext], principal: PrincipalRow) -> str:
    session_id = _transport_session_id(ctx)
    if session_id is not None:
        return f"principal:{principal.id}:session:{session_id}"
    # Stdio and stateless HTTP have no transport session id. Principal ownership is the
    # stable security boundary there; local stdio resolves to its configured local principal.
    return f"principal:{principal.id}:fallback"


@asynccontextmanager
async def _browser_session(
    ctx: Context[AppContext], principal: PrincipalRow
) -> AsyncIterator[BrowserController]:
    app = _app(ctx)
    async with app.interactive_browsers.use(_browser_session_key(ctx, principal)) as browser:
        yield browser


async def _principal(ctx: Context[AppContext]) -> PrincipalRow:
    app = _app(ctx)
    access_token = get_access_token()
    if access_token is not None:
        external_key = (access_token.claims or {}).get("skillwright_external_key")
        if isinstance(external_key, str):
            return await app.authorization.authenticated_principal(external_key)
    external_key = authenticated_principal(ctx.request_context)
    if external_key is not None:
        return await app.authorization.authenticated_principal(external_key)
    if app.settings.allow_unauthenticated_local:
        return await app.authorization.local_principal()
    raise AuthorizationError("authentication is required", code="authentication_required")


async def _skill(
    ctx: Context[AppContext],
    name: str,
    permission: SkillPermission,
) -> tuple[PrincipalRow, SkillRow]:
    app = _app(ctx)
    principal = await _principal(ctx)
    skill = await app.database.get_skill(name)
    if skill is None:
        raise KeyError(f"skill not found: {name}")
    await app.authorization.require_skill(principal, skill, permission)
    return principal, skill


async def _run(
    ctx: Context[AppContext],
    run_id: str,
    permission: SkillPermission,
) -> tuple[PrincipalRow, RunRow, SkillRow]:
    app = _app(ctx)
    principal = await _principal(ctx)
    run = await app.database.get_run(run_id)
    if run is None:
        raise KeyError(f"run not found: {run_id}")
    skill = await app.database.get_skill_by_id(run.skill_id)
    if skill is None:
        raise KeyError(f"skill for run not found: {run_id}")
    await app.authorization.require_skill(principal, skill, permission)
    return principal, run, skill


@mcp.tool()
async def browser_navigate(url: str, ctx: Context[AppContext]) -> dict[str, Any]:
    """Navigate the current browser to a URL and record the action in Skillwright history."""

    app = _app(ctx)
    principal = await _principal(ctx)
    app.authorization.require_global(principal, "browser")
    async with _browser_session(ctx, principal) as browser:
        return (await browser.navigate(url, actor_principal_id=principal.id)).as_dict()


@mcp.tool()
async def browser_snapshot(
    ctx: Context[AppContext],
    target: str | None = None,
    depth: int | None = None,
) -> dict[str, Any]:
    """Capture the current Playwright accessibility snapshot."""

    app = _app(ctx)
    principal = await _principal(ctx)
    app.authorization.require_global(principal, "browser")
    async with _browser_session(ctx, principal) as browser:
        return (
            await browser.snapshot(
                target=target,
                depth=depth,
                actor_principal_id=principal.id,
            )
        ).as_dict()


@mcp.tool()
async def browser_click(
    target: str,
    ctx: Context[AppContext],
    element: str | None = None,
    double_click: bool = False,
    button: Literal["left", "right", "middle"] = "left",
) -> dict[str, Any]:
    """Click an element identified by a current Playwright snapshot target."""

    app = _app(ctx)
    principal = await _principal(ctx)
    app.authorization.require_global(principal, "browser")
    async with _browser_session(ctx, principal) as browser:
        return (
            await browser.click(
                target,
                element=element,
                double_click=double_click,
                button=button,
                actor_principal_id=principal.id,
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

    app = _app(ctx)
    principal = await _principal(ctx)
    app.authorization.require_global(principal, "browser")
    async with _browser_session(ctx, principal) as browser:
        return (
            await browser.fill(
                target,
                text,
                element=element,
                submit=submit,
                actor_principal_id=principal.id,
            )
        ).as_dict()


@mcp.tool()
async def browser_fill_secret(
    target: str,
    secret_ref: str,
    input_name: str,
    ctx: Context[AppContext],
    provider: SecretProvider = "env",
    element: str | None = None,
    submit: bool = False,
) -> dict[str, Any]:
    """Fill a field from a server-side secret without returning or persisting its value."""

    app = _app(ctx)
    principal = await _principal(ctx)
    app.authorization.require_global(principal, "admin")
    try:
        normalized_ref = validate_secret_ref(secret_ref, provider=provider)
        secret_value = await app.engine.secret_resolver.resolve(normalized_ref, provider=provider)
    except (ValueError, SecretResolutionError) as exc:
        return {"ok": False, "error": str(exc)}
    async with _browser_session(ctx, principal) as browser:
        return (
            await browser.fill_secret(
                target,
                secret_value,
                secret_ref=normalized_ref,
                input_name=input_name,
                provider=provider,
                element=element,
                submit=submit,
                actor_principal_id=principal.id,
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

    app = _app(ctx)
    principal = await _principal(ctx)
    app.authorization.require_global(principal, "browser")
    async with _browser_session(ctx, principal) as browser:
        return (
            await browser.select(
                target,
                values,
                element=element,
                actor_principal_id=principal.id,
            )
        ).as_dict()


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
    app = _app(ctx)
    principal = await _principal(ctx)
    app.authorization.require_global(principal, "browser")
    async with _browser_session(ctx, principal) as browser:
        return (
            await browser.wait(
                seconds=seconds,
                text=text,
                text_gone=text_gone,
                actor_principal_id=principal.id,
            )
        ).as_dict()


@mcp.tool()
async def skill_record_start(
    name: str,
    ctx: Context[AppContext],
    description: str = "",
) -> dict[str, Any]:
    """Start recording subsequent proxied browser actions as a named reusable skill."""

    app = _app(ctx)
    principal = await _principal(ctx)
    app.authorization.require_global(principal, "create_skill")
    async with _browser_session(ctx, principal) as browser:
        skills = SkillService(app.database, browser, app.engine)
        return await skills.record_start(
            name,
            description,
            owner_principal_id=principal.id,
        )


@mcp.tool()
async def skill_record_stop(ctx: Context[AppContext]) -> dict[str, Any]:
    """Stop the active recording, compile it, validate it, and save a new skill version."""

    app = _app(ctx)
    principal = await _principal(ctx)
    app.authorization.require_global(principal, "create_skill")
    async with _browser_session(ctx, principal) as browser:
        skills = SkillService(app.database, browser, app.engine)
        return await skills.record_stop(owner_principal_id=principal.id)


@mcp.tool()
async def skill_save_from_history(
    name: str,
    start_event: int,
    end_event: int,
    ctx: Context[AppContext],
    description: str = "",
) -> dict[str, Any]:
    """Compile a prior contiguous browser-action range into a reusable skill."""

    app = _app(ctx)
    principal = await _principal(ctx)
    app.authorization.require_global(principal, "create_skill")
    return await app.skills.save_from_history(
        name,
        start_event=start_event,
        end_event=end_event,
        description=description,
        owner_principal_id=principal.id,
    )


@mcp.tool()
async def skill_list(ctx: Context[AppContext]) -> dict[str, Any]:
    """List persisted skills and their current versions."""

    app = _app(ctx)
    principal = await _principal(ctx)
    rows = await app.authorization.visible_skills(principal)
    return {
        "skills": [
            {
                "name": row.name,
                "description": row.description,
                "current_version": row.current_version,
            }
            for row in rows
            if await app.authorization.can_skill(principal, row, "view")
        ]
    }


@mcp.tool()
async def skill_search(
    query: str,
    ctx: Context[AppContext],
    limit: int = 10,
) -> dict[str, Any]:
    """Search persisted skills by name and description."""

    app = _app(ctx)
    principal = await _principal(ctx)
    needle = query.strip().casefold()
    rows = await app.authorization.visible_skills(principal)
    matches = [
        row
        for row in rows
        if await app.authorization.can_skill(principal, row, "view")
        and (needle in row.name.casefold() or needle in row.description.casefold())
    ][:limit]
    return {
        "query": query,
        "skills": [
            {
                "name": row.name,
                "description": row.description,
                "current_version": row.current_version,
            }
            for row in matches
        ],
    }


@mcp.tool()
async def skill_get(
    name: str,
    ctx: Context[AppContext],
    version: int | None = None,
) -> dict[str, Any]:
    """Get one persisted workflow definition."""

    await _skill(ctx, name, "view")
    return await _app(ctx).skills.get(name, version)


@mcp.tool()
async def skill_parameterize(
    name: str,
    bindings: list[ParameterBinding],
    ctx: Context[AppContext],
) -> dict[str, Any]:
    """Replace recorded literals with typed workflow inputs and save a new version."""

    principal, _ = await _skill(ctx, name, "edit")
    return await _app(ctx).skills.parameterize(
        name,
        bindings,
        actor_principal_id=principal.id,
    )


@mcp.tool()
async def skill_secret_bind(
    name: str,
    input_name: str,
    secret_ref: str,
    ctx: Context[AppContext],
    provider: SecretProvider = "env",
) -> dict[str, Any]:
    """Bind a secret workflow input to a server-managed secret reference."""

    app = _app(ctx)
    principal, _ = await _skill(ctx, name, "manage")
    app.authorization.require_global(principal, "admin")
    return await app.skills.bind_secret(
        name,
        input_name=input_name,
        secret_ref=secret_ref,
        provider=provider,
        principal_id=principal.id,
    )


@mcp.tool()
async def skill_secret_unbind(
    name: str,
    input_name: str,
    ctx: Context[AppContext],
) -> dict[str, Any]:
    """Remove a server-side binding for a secret workflow input."""

    app = _app(ctx)
    principal, _ = await _skill(ctx, name, "manage")
    app.authorization.require_global(principal, "admin")
    return await app.skills.unbind_secret(
        name,
        input_name=input_name,
        principal_id=principal.id,
    )


@mcp.tool()
async def skill_secret_status(name: str, ctx: Context[AppContext]) -> dict[str, Any]:
    """Show which secret inputs are configured without exposing references or values."""

    await _skill(ctx, name, "manage")
    return await _app(ctx).skills.secret_status(name)


@mcp.tool()
async def skill_approval_set(
    name: str,
    step: int,
    required: bool,
    ctx: Context[AppContext],
    reason: str | None = None,
) -> dict[str, Any]:
    """Add or remove a durable approval gate on a mutating workflow step."""

    principal, _ = await _skill(ctx, name, "edit")
    return await _app(ctx).skills.set_approval_gate(
        name,
        step=step,
        required=required,
        reason=reason,
        actor_principal_id=principal.id,
    )


@mcp.tool()
async def skill_run(
    name: str,
    ctx: Context[AppContext],
    inputs: dict[str, Any] | None = None,
    version: int | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Queue a deterministic browser skill run (or execute inline in local development)."""

    app = _app(ctx)
    principal, _ = await _skill(ctx, name, "run")
    return await app.dispatcher.submit(
        name,
        inputs=inputs,
        version=version,
        idempotency_key=idempotency_key,
        requested_by_principal_id=principal.id,
    )


@mcp.tool()
async def skill_status(run_id: str, ctx: Context[AppContext]) -> dict[str, Any]:
    """Inspect persisted status and failure context for a workflow run."""

    await _run(ctx, run_id, "view")
    return await _app(ctx).skills.status(run_id)


@mcp.tool()
async def skill_cancel(run_id: str, ctx: Context[AppContext]) -> dict[str, Any]:
    """Request cancellation of a queued or running skill execution."""

    principal, _, _ = await _run(ctx, run_id, "run")
    return await _app(ctx).dispatcher.cancel(
        run_id,
        actor_principal_id=principal.id,
    )


@mcp.tool()
async def skill_repair(
    run_id: str,
    step: int,
    replacement_element_id: str,
    ctx: Context[AppContext],
    persist: bool = True,
) -> dict[str, Any]:
    """Apply one candidate target repair, continue the run, and optionally save a new version."""

    principal, _, _ = await _run(ctx, run_id, "edit")
    return await _app(ctx).dispatcher.repair(
        run_id,
        step=step,
        replacement_element_id=replacement_element_id,
        persist=persist,
        actor_principal_id=principal.id,
    )


@mcp.tool()
async def skill_approval_decide(
    approval_id: str,
    approve: bool,
    ctx: Context[AppContext],
    comment: str | None = None,
) -> dict[str, Any]:
    """Approve or reject a pending gated workflow step."""

    app = _app(ctx)
    principal = await _principal(ctx)
    approval = await app.database.get_approval(approval_id)
    if approval is None:
        raise KeyError(f"approval not found: {approval_id}")
    run = await app.database.get_run(approval.run_id)
    if run is None:
        raise KeyError(f"run not found for approval: {approval.run_id}")
    skill = await app.database.get_skill_by_id(run.skill_id)
    if skill is None:
        raise KeyError(f"skill not found for approval: {approval_id}")
    await app.authorization.require_skill(principal, skill, "approve")
    result = await app.dispatcher.decide_approval(
        approval_id,
        approve=approve,
        decided_by_principal_id=principal.id,
        comment=comment,
    )
    if result.get("status") != "invalid_approval":
        await app.database.audit(
            "approval.decided",
            principal_id=principal.id,
            entity_type="approval",
            entity_id=approval.id,
            data={"approve": approve, "run_id": approval.run_id},
        )
    return result


@mcp.tool()
async def skill_versions(name: str, ctx: Context[AppContext]) -> dict[str, Any]:
    """List immutable versions of a persisted skill."""

    await _skill(ctx, name, "view")
    return await _app(ctx).skills.versions(name)


@mcp.tool()
async def skill_rollback(
    name: str,
    version: int,
    ctx: Context[AppContext],
) -> dict[str, Any]:
    """Create a new current version whose definition matches an older skill version."""

    principal, _ = await _skill(ctx, name, "edit")
    return await _app(ctx).skills.rollback(
        name,
        version,
        actor_principal_id=principal.id,
    )


@mcp.tool()
async def principal_set_role(
    external_key: str,
    role: Role,
    ctx: Context[AppContext],
) -> dict[str, Any]:
    """Create or update a Skillwright principal role. Admin only."""

    app = _app(ctx)
    actor = await _principal(ctx)
    app.authorization.require_global(actor, "admin")
    principal = await app.database.set_principal_role(external_key, role)
    await app.database.audit(
        "principal.role.updated",
        principal_id=actor.id,
        entity_type="principal",
        entity_id=principal.id,
        data={"role": role},
    )
    return {"status": "saved", "principal": external_key, "role": principal.role}


@mcp.tool()
async def principal_set_disabled(
    external_key: str,
    disabled: bool,
    ctx: Context[AppContext],
) -> dict[str, Any]:
    """Enable or disable a provisioned principal. Admin only."""

    app = _app(ctx)
    actor = await _principal(ctx)
    app.authorization.require_global(actor, "admin")
    principal = await app.database.set_principal_disabled(external_key, disabled)
    await app.database.audit(
        "principal.disabled.updated",
        principal_id=actor.id,
        entity_type="principal",
        entity_id=principal.id,
        data={"disabled": disabled},
    )
    return {"status": "saved", "principal": external_key, "disabled": disabled}


@mcp.tool()
async def skill_access_grant(
    name: str,
    principal_external_key: str,
    permission: SkillPermission,
    ctx: Context[AppContext],
) -> dict[str, Any]:
    """Grant one per-skill permission to a provisioned principal."""

    app = _app(ctx)
    actor, skill = await _skill(ctx, name, "manage")
    target = await app.database.get_principal_by_external_key(principal_external_key)
    if target is None:
        raise KeyError(f"principal not found: {principal_external_key}")
    await app.authorization.grant(actor, skill, target, permission)
    await app.database.audit(
        "skill.permission.granted",
        principal_id=actor.id,
        entity_type="skill",
        entity_id=skill.id,
        data={"target_principal_id": target.id, "permission": permission},
    )
    return {"status": "saved", "skill": name, "permission": permission}


@mcp.tool()
async def skill_access_revoke(
    name: str,
    principal_external_key: str,
    permission: SkillPermission,
    ctx: Context[AppContext],
) -> dict[str, Any]:
    """Revoke one per-skill permission from a principal."""

    app = _app(ctx)
    actor, skill = await _skill(ctx, name, "manage")
    target = await app.database.get_principal_by_external_key(principal_external_key)
    if target is None:
        raise KeyError(f"principal not found: {principal_external_key}")
    removed = await app.authorization.revoke(actor, skill, target, permission)
    await app.database.audit(
        "skill.permission.revoked",
        principal_id=actor.id,
        entity_type="skill",
        entity_id=skill.id,
        data={"target_principal_id": target.id, "permission": permission, "removed": removed},
    )
    return {"status": "saved", "skill": name, "permission": permission, "removed": removed}


@mcp.tool()
async def skill_access_get(name: str, ctx: Context[AppContext]) -> dict[str, Any]:
    """Inspect per-skill grants. Requires manage permission."""

    app = _app(ctx)
    _, skill = await _skill(ctx, name, "manage")
    rows = await app.database.list_skill_permissions(skill.id)
    grants = []
    for row in rows:
        principal = await app.database.get_principal(row.principal_id)
        if principal is not None:
            grants.append(
                {
                    "principal": principal.external_key,
                    "role": principal.role,
                    "permission": row.permission,
                }
            )
    return {"status": "found", "skill": name, "grants": grants}
