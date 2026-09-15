from __future__ import annotations

import hashlib
import hmac
import re
from typing import Literal

from mcp.server.auth.provider import AccessToken

from .config import Settings
from .db import Database, PrincipalRow, SkillRow

Role = Literal["admin", "developer", "viewer"]
SkillPermission = Literal["view", "run", "edit", "approve", "manage"]
GlobalCapability = Literal["browser", "create_skill", "admin"]

ROLES: frozenset[str] = frozenset({"admin", "developer", "viewer"})
SKILL_PERMISSIONS: frozenset[str] = frozenset({"view", "run", "edit", "approve", "manage"})
_SHA256_HEX_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class AuthorizationError(PermissionError):
    def __init__(self, message: str, *, code: str = "permission_denied") -> None:
        super().__init__(message)
        self.code = code


class BearerTokenAuthenticator:
    """Map opaque bearer-token SHA-256 digests to provisioned principal keys."""

    def __init__(self, token_hashes: dict[str, str]) -> None:
        normalized: list[tuple[str, str]] = []
        for token_hash, principal_key in token_hashes.items():
            if _SHA256_HEX_RE.fullmatch(token_hash) is None:
                raise ValueError("auth token hashes must be 64-character SHA-256 hex digests")
            if not principal_key.strip():
                raise ValueError("auth token principal keys must be non-empty")
            normalized.append((token_hash.lower(), principal_key))
        self._token_hashes = tuple(normalized)

    @property
    def configured(self) -> bool:
        return bool(self._token_hashes)

    def principal_for_token(self, token: str) -> str | None:
        if not token:
            return None
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        for expected_digest, principal_key in self._token_hashes:
            if hmac.compare_digest(digest, expected_digest):
                return principal_key
        return None


class MCPBearerTokenVerifier:
    """Adapt Skillwright's opaque service-token map to the MCP SDK bearer verifier."""

    def __init__(
        self,
        authenticator: BearerTokenAuthenticator,
        *,
        issuer: str,
        resource: str | None,
    ) -> None:
        self.authenticator = authenticator
        self.issuer = issuer
        self.resource = resource

    async def verify_token(self, token: str) -> AccessToken | None:
        principal_key = self.authenticator.principal_for_token(token)
        if principal_key is None:
            return None
        return AccessToken(
            token="[REDACTED]",
            client_id="skillwright-service-token",
            scopes=["skillwright"],
            resource=self.resource,
            subject=principal_key,
            claims={
                "iss": self.issuer,
                "skillwright_external_key": principal_key,
            },
        )


class AuthorizationService:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings

    async def local_principal(self) -> PrincipalRow:
        principal = await self.database.ensure_principal(
            self.settings.local_principal,
            self.settings.local_role,
        )
        return self._require_enabled(principal)

    async def authenticated_principal(self, external_key: str) -> PrincipalRow:
        principal = await self.database.get_principal_by_external_key(external_key)
        if principal is None:
            raise AuthorizationError(
                "authenticated principal is not provisioned in Skillwright",
                code="unknown_principal",
            )
        return self._require_enabled(principal)

    async def principal_by_id(self, principal_id: str) -> PrincipalRow:
        principal = await self.database.get_principal(principal_id)
        if principal is None:
            raise AuthorizationError("principal no longer exists", code="unknown_principal")
        return self._require_enabled(principal)

    def require_global(self, principal: PrincipalRow, capability: GlobalCapability) -> None:
        self._require_enabled(principal)
        if principal.role == "admin":
            return
        if capability in {"browser", "create_skill"} and principal.role == "developer":
            return
        raise AuthorizationError(
            f"role {principal.role!r} cannot use capability {capability!r}",
        )

    async def can_skill(
        self,
        principal: PrincipalRow,
        skill: SkillRow,
        permission: SkillPermission,
    ) -> bool:
        self._require_enabled(principal)
        if principal.role == "admin":
            return True
        if permission != "view" and principal.role != "developer":
            return False
        if skill.owner_principal_id == principal.id:
            return True
        permissions = await self.database.skill_permissions(skill.id, principal.id)
        return permission in permissions

    async def require_skill(
        self,
        principal: PrincipalRow,
        skill: SkillRow,
        permission: SkillPermission,
    ) -> None:
        if not await self.can_skill(principal, skill, permission):
            raise AuthorizationError(
                f"principal cannot {permission} skill {skill.name!r}",
            )

    async def visible_skills(self, principal: PrincipalRow) -> list[SkillRow]:
        self._require_enabled(principal)
        return list(await self.database.list_skills_for_principal(principal))

    async def grant(
        self,
        actor: PrincipalRow,
        skill: SkillRow,
        target: PrincipalRow,
        permission: SkillPermission,
    ) -> None:
        await self.require_skill(actor, skill, "manage")
        await self.database.grant_skill_permission(skill.id, target.id, permission)

    async def revoke(
        self,
        actor: PrincipalRow,
        skill: SkillRow,
        target: PrincipalRow,
        permission: SkillPermission,
    ) -> bool:
        await self.require_skill(actor, skill, "manage")
        return await self.database.revoke_skill_permission(skill.id, target.id, permission)

    @staticmethod
    def _require_enabled(principal: PrincipalRow) -> PrincipalRow:
        if principal.disabled:
            raise AuthorizationError("principal is disabled", code="principal_disabled")
        if principal.role not in ROLES:
            raise AuthorizationError("principal has an invalid role", code="invalid_role")
        return principal
