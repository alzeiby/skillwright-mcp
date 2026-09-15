from __future__ import annotations

import hashlib
import hmac
import re

from mcp.server.auth.provider import AccessToken

from .config import Settings
from .db import Database, PrincipalRow

_SHA256_HEX_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class AuthenticationError(PermissionError):
    def __init__(self, message: str, *, code: str = "authentication_required") -> None:
        super().__init__(message)
        self.code = code


class BearerTokenAuthenticator:
    """Map opaque bearer-token SHA-256 digests to principal keys."""

    def __init__(self, token_hashes: dict[str, str]) -> None:
        normalized: list[tuple[str, str]] = []
        seen_hashes: set[str] = set()
        for token_hash, principal_key in token_hashes.items():
            if _SHA256_HEX_RE.fullmatch(token_hash) is None:
                raise ValueError("auth token hashes must be 64-character SHA-256 hex digests")
            if not principal_key.strip():
                raise ValueError("auth token principal keys must be non-empty")
            normalized_hash = token_hash.lower()
            if normalized_hash in seen_hashes:
                raise ValueError("auth token hashes must be unique")
            seen_hashes.add(normalized_hash)
            normalized.append((normalized_hash, principal_key))
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


class IdentityService:
    """Resolve authenticated identities to stable principal rows for attribution."""

    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings

    async def local_principal(self) -> PrincipalRow:
        return await self.database.ensure_principal(self.settings.local_principal)

    async def authenticated_principal(self, external_key: str) -> PrincipalRow:
        # Presence in the configured bearer-token map is the authentication boundary. Principal
        # rows are retained only for attribution/auditing; there are no roles or per-skill ACLs.
        return await self.database.ensure_principal(external_key)
