from __future__ import annotations

import hashlib
from typing import Any

import pytest

from skillwright_mcp.auth import BearerTokenAuthenticator, IdentityService, MCPBearerTokenVerifier
from skillwright_mcp.config import Settings
from skillwright_mcp.db import Database, PrincipalRow
from skillwright_mcp.workflow import WorkflowDefinition


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


def test_principal_model_has_identity_fields_only() -> None:
    assert set(PrincipalRow.__table__.columns.keys()) == {"id", "external_key", "created_at"}


@pytest.mark.asyncio
async def test_identity_service_auto_provisions_stable_principals(tmp_path: Any) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'identity.db').as_posix()}")
    await database.initialize(create_schema=True)
    identity = IdentityService(
        database,
        Settings(
            database_url=database.url,
            execution_backend="inline",
            local_principal="local@example.test",
        ),
    )
    try:
        local = await identity.local_principal()
        authenticated = await identity.authenticated_principal("service@example.test")
        authenticated_again = await identity.authenticated_principal("service@example.test")

        assert local.external_key == "local@example.test"
        assert authenticated.external_key == "service@example.test"
        assert authenticated_again.id == authenticated.id
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_authenticated_identities_share_skill_capabilities(tmp_path: Any) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'flat-access.db').as_posix()}")
    await database.initialize(create_schema=True)
    try:
        alice = await database.ensure_principal("alice@example.test")
        bob = await database.ensure_principal("bob@example.test")
        workflow = WorkflowDefinition.model_validate(
            {
                "name": "shared-skill",
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
                "steps": [{"op": "navigate", "url": "https://example.test/v2"}],
            }
        )

        _, second = await database.create_skill_version(
            changed,
            actor_principal_id=bob.id,
            expected_current_version=first.version,
        )
        stored = await database.get_skill(workflow.name)

        assert second.version == 2
        assert stored is not None
        assert stored.owner_principal_id == skill.owner_principal_id == alice.id
        assert stored.current_version == 2
    finally:
        await database.close()
