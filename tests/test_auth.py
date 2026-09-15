from __future__ import annotations

import hashlib
from typing import Any, cast

import pytest
from sqlalchemy import func, select

from skillwright_mcp.auth import (
    AuthorizationError,
    AuthorizationService,
    BearerTokenAuthenticator,
    MCPBearerTokenVerifier,
)
from skillwright_mcp.browser import BrowserController
from skillwright_mcp.config import Settings
from skillwright_mcp.db import BrowserActionRow, Database
from skillwright_mcp.engine import WorkflowEngine
from skillwright_mcp.runtime import build_runtime
from skillwright_mcp.workflow import WorkflowDefinition


class NeverPlaywright:
    async def call(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("browser must not be reached after authorization is revoked")

    async def has_tool(self, _name: str) -> bool:
        return False


@pytest.mark.asyncio
async def test_hashed_bearer_authenticator_and_mcp_verifier_do_not_retain_token() -> None:
    token = "opaque-test-service-token"
    digest = hashlib.sha256(token.encode()).hexdigest()
    authenticator = BearerTokenAuthenticator({digest: "service@example.test"})
    verifier = MCPBearerTokenVerifier(
        authenticator,
        issuer="https://auth.example.test",
        resource="https://skillwright.example.test/mcp",
    )

    assert authenticator.principal_for_token(token) == "service@example.test"
    assert authenticator.principal_for_token("wrong") is None
    verified = await verifier.verify_token(token)
    assert verified is not None
    assert verified.token == "[REDACTED]"
    assert verified.subject == "service@example.test"
    assert verified.resource == "https://skillwright.example.test/mcp"
    assert verified.claims == {
        "iss": "https://auth.example.test",
        "skillwright_external_key": "service@example.test",
    }


def test_hashed_bearer_authenticator_rejects_case_variant_duplicate_digest() -> None:
    digest = "ab" * 32
    with pytest.raises(ValueError, match="unique"):
        BearerTokenAuthenticator(
            {
                digest: "first@example.test",
                digest.upper(): "second@example.test",
            }
        )


@pytest.mark.asyncio
async def test_bootstrap_admin_is_created_once_without_repromoting_existing_principal(
    tmp_path: Any,
) -> None:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'bootstrap-auth.db').as_posix()}"
    settings = Settings(
        database_url=database_url,
        database_auto_create_schema=True,
        execution_backend="inline",
        allow_unauthenticated_local=False,
        bootstrap_admin_principal="bootstrap@example.test",
    )
    runtime = build_runtime(settings)
    try:
        await runtime.start()
        created = await runtime.database.get_principal_by_external_key("bootstrap@example.test")
        assert created is not None
        assert created.role == "admin"
    finally:
        await runtime.close()

    database = Database(database_url)
    try:
        await database.set_principal_role("bootstrap@example.test", "viewer")
    finally:
        await database.close()

    runtime = build_runtime(settings)
    try:
        await runtime.start()
        existing = await runtime.database.get_principal_by_external_key("bootstrap@example.test")
        assert existing is not None
        assert existing.role == "viewer"
    finally:
        await runtime.close()


async def _setup(tmp_path: Any) -> tuple[Database, AuthorizationService]:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'auth.db').as_posix()}")
    await database.initialize(create_schema=True)
    settings = Settings(
        database_url=database.url,
        execution_backend="inline",
        allow_unauthenticated_local=True,
        local_principal="admin@example.test",
        local_role="admin",
    )
    return database, AuthorizationService(database, settings)


@pytest.mark.asyncio
async def test_skill_permissions_roles_and_disabled_principals(tmp_path: Any) -> None:
    database, authorization = await _setup(tmp_path)
    try:
        admin = await database.ensure_principal("admin@example.test", "admin")
        owner = await database.ensure_principal("owner@example.test", "developer")
        collaborator = await database.ensure_principal("collaborator@example.test", "developer")
        viewer = await database.ensure_principal("viewer@example.test", "viewer")
        workflow = WorkflowDefinition.model_validate(
            {
                "name": "private-skill",
                "steps": [{"op": "navigate", "url": "https://example.com"}],
            }
        )
        skill, _ = await database.create_skill_version(
            workflow,
            owner_principal_id=owner.id,
        )

        await authorization.require_skill(admin, skill, "manage")
        await authorization.require_skill(owner, skill, "manage")
        assert not await authorization.can_skill(collaborator, skill, "view")

        await authorization.grant(owner, skill, collaborator, "view")
        assert await authorization.can_skill(collaborator, skill, "view")
        assert not await authorization.can_skill(collaborator, skill, "run")

        await authorization.grant(owner, skill, collaborator, "run")
        assert await authorization.can_skill(collaborator, skill, "run")

        await database.grant_skill_permission(skill.id, viewer.id, "run")
        assert not await authorization.can_skill(viewer, skill, "run")

        await database.set_principal_disabled("collaborator@example.test", True)
        disabled = await database.get_principal(collaborator.id)
        assert disabled is not None
        with pytest.raises(AuthorizationError, match="disabled"):
            await authorization.require_skill(disabled, skill, "view")
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_visible_skills_are_owner_or_granted(tmp_path: Any) -> None:
    database, authorization = await _setup(tmp_path)
    try:
        owner = await database.ensure_principal("owner@example.test", "developer")
        other = await database.ensure_principal("other@example.test", "developer")
        shared = await database.ensure_principal("shared@example.test", "developer")

        first, _ = await database.create_skill_version(
            WorkflowDefinition.model_validate(
                {"name": "owned", "steps": [{"op": "navigate", "url": "https://a.test"}]}
            ),
            owner_principal_id=owner.id,
        )
        second, _ = await database.create_skill_version(
            WorkflowDefinition.model_validate(
                {"name": "other", "steps": [{"op": "navigate", "url": "https://b.test"}]}
            ),
            owner_principal_id=other.id,
        )
        await database.grant_skill_permission(second.id, shared.id, "view")

        assert {row.name for row in await authorization.visible_skills(owner)} == {first.name}
        assert {row.name for row in await authorization.visible_skills(shared)} == {second.name}
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_existing_skill_version_write_requires_atomic_edit_permission(tmp_path: Any) -> None:
    database, _authorization = await _setup(tmp_path)
    try:
        alice = await database.ensure_principal("alice@example.test", "developer")
        bob = await database.ensure_principal("bob@example.test", "developer")
        workflow = WorkflowDefinition.model_validate(
            {
                "name": "owned-versioned-skill",
                "steps": [{"op": "navigate", "url": "https://example.test/v1"}],
            }
        )
        skill, first = await database.create_skill_version(
            workflow,
            owner_principal_id=alice.id,
            actor_principal_id=alice.id,
        )

        changed = WorkflowDefinition.model_validate(
            {
                "name": workflow.name,
                "steps": [{"op": "navigate", "url": "https://example.test/bob"}],
            }
        )
        with pytest.raises(PermissionError, match="cannot edit existing skill"):
            await database.create_skill_version(
                changed,
                owner_principal_id=bob.id,
                actor_principal_id=bob.id,
                expected_current_version=first.version,
            )

        unchanged = await database.get_skill(workflow.name)
        assert unchanged is not None
        assert unchanged.owner_principal_id == alice.id
        assert unchanged.current_version == 1

        await database.grant_skill_permission(skill.id, bob.id, "edit")
        _, second = await database.create_skill_version(
            changed,
            owner_principal_id=bob.id,
            actor_principal_id=bob.id,
            expected_current_version=1,
        )
        assert second.version == 2
        still_alice = await database.get_skill(workflow.name)
        assert still_alice is not None
        assert still_alice.owner_principal_id == alice.id
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_legacy_unowned_skill_is_not_silently_claimed_by_developer(tmp_path: Any) -> None:
    database, _authorization = await _setup(tmp_path)
    try:
        developer = await database.ensure_principal("developer@example.test", "developer")
        admin = await database.ensure_principal("legacy-admin@example.test", "admin")
        workflow = WorkflowDefinition.model_validate(
            {
                "name": "legacy-unowned",
                "steps": [{"op": "navigate", "url": "https://legacy.test"}],
            }
        )
        skill, _ = await database.create_skill_version(workflow)
        assert skill.owner_principal_id is None

        with pytest.raises(PermissionError, match="cannot edit existing skill"):
            await database.create_skill_version(
                workflow,
                owner_principal_id=developer.id,
                actor_principal_id=developer.id,
                expected_current_version=1,
            )
        after_developer = await database.get_skill(workflow.name)
        assert after_developer is not None
        assert after_developer.owner_principal_id is None
        assert after_developer.current_version == 1

        _, second = await database.create_skill_version(
            workflow,
            actor_principal_id=admin.id,
            expected_current_version=1,
        )
        assert second.version == 2
        after_admin = await database.get_skill(workflow.name)
        assert after_admin is not None
        assert after_admin.owner_principal_id is None
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_atomic_version_write_rejects_demoted_owner_and_editor(tmp_path: Any) -> None:
    database, authorization = await _setup(tmp_path)
    try:
        owner = await database.ensure_principal("owner-demoted@example.test", "developer")
        editor = await database.ensure_principal("editor-demoted@example.test", "developer")
        workflow = WorkflowDefinition.model_validate(
            {
                "name": "role-sensitive-version-write",
                "steps": [{"op": "navigate", "url": "https://example.test/v1"}],
            }
        )
        skill, first = await database.create_skill_version(
            workflow,
            owner_principal_id=owner.id,
            actor_principal_id=owner.id,
        )
        await database.grant_skill_permission(skill.id, editor.id, "edit")
        owner = await database.set_principal_role(owner.external_key, "viewer")
        editor = await database.set_principal_role(editor.external_key, "viewer")

        assert await authorization.can_skill(owner, skill, "edit") is False
        assert await authorization.can_skill(editor, skill, "edit") is False
        for actor in (owner, editor):
            with pytest.raises(PermissionError, match="cannot edit existing skill"):
                await database.create_skill_version(
                    workflow,
                    actor_principal_id=actor.id,
                    expected_current_version=first.version,
                )

        stored = await database.get_skill(workflow.name)
        assert stored is not None
        assert stored.current_version == first.version
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_permission_revoked_after_queue_prevents_browser_work(tmp_path: Any) -> None:
    database, authorization = await _setup(tmp_path)
    try:
        owner = await database.ensure_principal("owner@example.test", "developer")
        runner = await database.ensure_principal("runner@example.test", "developer")
        workflow = WorkflowDefinition.model_validate(
            {
                "name": "revocable-run",
                "steps": [{"op": "navigate", "url": "https://example.com"}],
            }
        )
        skill, _ = await database.create_skill_version(
            workflow,
            owner_principal_id=owner.id,
        )
        await database.grant_skill_permission(skill.id, runner.id, "run")
        browser = BrowserController(cast(Any, NeverPlaywright()), database)
        engine = WorkflowEngine(database, browser, authorization)

        prepared = await engine.prepare_run(
            workflow.name,
            idempotency_key="revoked-after-queue",
            requested_by_principal_id=runner.id,
        )
        assert prepared["status"] == "queued"
        await database.revoke_skill_permission(skill.id, runner.id, "run")

        result = await engine.execute_persisted_run(
            str(prepared["run_id"]),
            worker_id="worker-test",
        )
        assert result["status"] == "failed"
        assert result["reason"] == "permission_revoked_before_execution"
        assert result["side_effect_state"] == "not_started"

        async with database.sessions() as session:
            action_count = await session.scalar(
                select(func.count()).select_from(BrowserActionRow).where(
                    BrowserActionRow.run_id == prepared["run_id"]
                )
            )
        assert action_count == 0
    finally:
        await database.close()
