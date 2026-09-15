from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy import select

from .auth import AuthorizationService, BearerTokenAuthenticator
from .browser import BrowserController
from .config import Settings
from .db import Database, RunRow
from .engine import WorkflowEngine
from .playwright import PlaywrightMCPClient
from .queue import RunDispatcher
from .skills import SkillService


@dataclass(slots=True)
class Runtime:
    settings: Settings
    database: Database
    playwright: PlaywrightMCPClient
    browser: BrowserController
    engine: WorkflowEngine
    skills: SkillService
    dispatcher: RunDispatcher
    authorization: AuthorizationService
    bearer_auth: BearerTokenAuthenticator
    _database_initialized: bool = False
    _local_principal_initialized: bool = False

    async def start(self) -> None:
        if not self._database_initialized:
            await self.database.initialize(
                create_schema=self.settings.database_auto_create_schema,
            )
            self._database_initialized = True
        if self.settings.bootstrap_admin_principal:
            existing = await self.database.get_principal_by_external_key(
                self.settings.bootstrap_admin_principal
            )
            if existing is None:
                await self.database.ensure_principal(
                    self.settings.bootstrap_admin_principal,
                    "admin",
                )
        if self.settings.allow_unauthenticated_local and not self._local_principal_initialized:
            await self.authorization.local_principal()
            self._local_principal_initialized = True
        await self.dispatcher.start()

    async def close(self) -> None:
        await self.dispatcher.close()
        await self.browser.close()
        await self.database.close()

    async def readiness(self) -> dict[str, str]:
        checks = {"database": "unavailable"}
        try:
            async with asyncio.timeout(self.settings.healthcheck_timeout_seconds):
                if not self._database_initialized:
                    await self.database.initialize(
                        create_schema=self.settings.database_auto_create_schema,
                    )
                    self._database_initialized = True
                async with self.database.sessions() as session:
                    await session.execute(select(RunRow).limit(1))
                if (
                    self.settings.allow_unauthenticated_local
                    and not self._local_principal_initialized
                ):
                    await self.authorization.local_principal()
                    self._local_principal_initialized = True
            checks["database"] = "ok"
        except Exception:
            return checks

        if self.settings.execution_backend == "redis":
            checks["redis"] = "unavailable"
            client = Redis.from_url(
                self.settings.redis_url,
                socket_connect_timeout=self.settings.healthcheck_timeout_seconds,
                socket_timeout=self.settings.healthcheck_timeout_seconds,
            )
            try:
                async with asyncio.timeout(self.settings.healthcheck_timeout_seconds):
                    await client.ping()
                    await self.dispatcher.start()
                checks["redis"] = "ok"
            except Exception:
                pass
            finally:
                await client.aclose()

        return checks


def build_runtime(settings: Settings | None = None) -> Runtime:
    resolved = settings or Settings()
    database = Database(resolved.database_url)
    authorization = AuthorizationService(database, resolved)
    bearer_auth = BearerTokenAuthenticator(resolved.auth_token_hashes)
    playwright = PlaywrightMCPClient(resolved)
    browser = BrowserController(playwright, database)
    engine = WorkflowEngine(database, browser, authorization)
    skills = SkillService(database, browser, engine)
    dispatcher = RunDispatcher(settings=resolved, database=database, engine=engine)
    return Runtime(
        settings=resolved,
        database=database,
        playwright=playwright,
        browser=browser,
        engine=engine,
        skills=skills,
        dispatcher=dispatcher,
        authorization=authorization,
        bearer_auth=bearer_auth,
    )


@asynccontextmanager
async def runtime_lifespan(settings: Settings | None = None) -> AsyncIterator[Runtime]:
    runtime = build_runtime(settings)
    await runtime.start()
    try:
        yield runtime
    finally:
        await runtime.close()
