from __future__ import annotations

from typing import Literal

from .config import Settings
from .db import Database, PrincipalRow, SkillRow

Role = Literal["admin", "developer", "viewer"]
SkillPermission = Literal["view", "run", "edit", "approve", "manage"]
GlobalCapability = Literal["browser", "create_skill", "admin"]

ROLES: frozenset[str] = frozenset({"admin", "developer", "viewer"})
SKILL_PERMISSIONS: frozenset[str] = frozenset({"view", "run", "edit", "approve", "manage"})


class AuthorizationError(PermissionError):
    def __init__(self, message: str, *, code: str = "permission_denied") -> None:
        super().__init__(message)
        self.code = code


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
