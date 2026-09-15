from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from time import monotonic

from redis.asyncio import Redis
from sqlalchemy import select, text

from .auth import BearerTokenAuthenticator, IdentityService
from .browser import BrowserController
from .config import Settings
from .db import SCHEMA_REVISION, Database, RunRow
from .engine import WorkflowEngine
from .playwright import PlaywrightMCPClient
from .queue import RunDispatcher
from .secrets import SecretResolver
from .skills import SkillService

INTERACTIVE_BROWSER_IDLE_TIMEOUT_SECONDS = 30 * 60.0
_INTERACTIVE_BROWSER_CLEANUP_INTERVAL_SECONDS = 60.0


@dataclass(slots=True)
class _BrowserSessionEntry:
    browser: BrowserController
    last_used: float
    leases: int = 0
    operation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class BrowserSessionPool:
    """Own one interactive Playwright subprocess per MCP session/fallback key.

    The MCP SDK expires idle Streamable HTTP sessions after 30 minutes by default but does
    not currently expose an application teardown callback. This pool mirrors that bound with
    its own sweeper, so an abandoned transport cannot retain a Playwright subprocess or secret
    redaction state indefinitely. Runtime shutdown closes every remaining entry explicitly.
    """

    def __init__(
        self,
        browser_factory: Callable[[str], BrowserController],
        *,
        idle_timeout_seconds: float = INTERACTIVE_BROWSER_IDLE_TIMEOUT_SECONDS,
        cleanup_interval_seconds: float = _INTERACTIVE_BROWSER_CLEANUP_INTERVAL_SECONDS,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if idle_timeout_seconds <= 0:
            raise ValueError("idle_timeout_seconds must be positive")
        if cleanup_interval_seconds <= 0:
            raise ValueError("cleanup_interval_seconds must be positive")
        self._browser_factory = browser_factory
        self._idle_timeout_seconds = idle_timeout_seconds
        self._cleanup_interval_seconds = min(cleanup_interval_seconds, idle_timeout_seconds)
        self._clock = clock
        self._entries: dict[str, _BrowserSessionEntry] = {}
        self._lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task[None] | None = None
        self._closed = False

    def start(self) -> None:
        if self._closed:
            raise RuntimeError("browser session pool is closed")
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(
                self._cleanup_loop(),
                name="skillwright-browser-session-cleanup",
            )

    @asynccontextmanager
    async def use(self, key: str) -> AsyncIterator[BrowserController]:
        """Lease the browser owned by one MCP session or authenticated fallback key."""

        self.start()
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = _BrowserSessionEntry(
                    browser=self._browser_factory(key),
                    last_used=self._clock(),
                )
                self._entries[key] = entry
            entry.leases += 1
            entry.last_used = self._clock()
        acquired = False
        try:
            await entry.operation_lock.acquire()
            acquired = True
            yield entry.browser
        finally:
            if acquired:
                entry.operation_lock.release()
            async with self._lock:
                current = self._entries.get(key)
                if current is entry:
                    current.leases -= 1
                    current.last_used = self._clock()

    async def cleanup_idle(self) -> None:
        """Close unleased session browsers that exceeded the bounded idle lifetime."""

        cutoff = self._clock() - self._idle_timeout_seconds
        stale: list[BrowserController] = []
        async with self._lock:
            for key, entry in list(self._entries.items()):
                if entry.leases == 0 and entry.last_used <= cutoff:
                    stale.append(entry.browser)
                    del self._entries[key]
        if stale:
            await asyncio.gather(*(browser.close() for browser in stale), return_exceptions=True)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        cleanup_task, self._cleanup_task = self._cleanup_task, None
        if cleanup_task is not None:
            cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await cleanup_task
        async with self._lock:
            browsers = [entry.browser for entry in self._entries.values()]
            self._entries.clear()
        if browsers:
            await asyncio.gather(*(browser.close() for browser in browsers), return_exceptions=True)

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(self._cleanup_interval_seconds)
            await self.cleanup_idle()


@dataclass(slots=True)
class Runtime:
    settings: Settings
    database: Database
    playwright: PlaywrightMCPClient
    browser: BrowserController
    engine: WorkflowEngine
    skills: SkillService
    dispatcher: RunDispatcher
    identity: IdentityService
    bearer_auth: BearerTokenAuthenticator
    interactive_browsers: BrowserSessionPool
    _database_initialized: bool = False
    _local_principal_initialized: bool = False

    async def start(self) -> None:
        if not self._database_initialized:
            await self.database.initialize(
                create_schema=self.settings.database_auto_create_schema,
            )
            self._database_initialized = True
        if self.settings.allow_unauthenticated_local and not self._local_principal_initialized:
            await self.identity.local_principal()
            self._local_principal_initialized = True
        self.interactive_browsers.start()
        await self.dispatcher.start()

    async def close(self) -> None:
        await self.dispatcher.close()
        await self.interactive_browsers.close()
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
                    await self.identity.local_principal()
                    self._local_principal_initialized = True
            checks["database"] = "ok"
        except Exception:
            return checks

        if not self.settings.database_auto_create_schema:
            checks["schema"] = "unavailable"
            try:
                async with asyncio.timeout(self.settings.healthcheck_timeout_seconds):
                    async with self.database.sessions() as session:
                        revision = await session.scalar(
                            text("SELECT version_num FROM alembic_version")
                        )
                checks["schema"] = "ok" if revision == SCHEMA_REVISION else "outdated"
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
    database = Database(
        resolved.resolved_database_url(),
        connect_args=resolved.resolved_database_connect_args(),
    )
    identity = IdentityService(database, resolved)
    bearer_auth = BearerTokenAuthenticator(resolved.auth_token_hashes)
    playwright = PlaywrightMCPClient(resolved)
    browser = BrowserController(playwright, database)
    engine = WorkflowEngine(
        database,
        browser,
        secret_resolver=SecretResolver(),
    )
    skills = SkillService(database, browser, engine)
    dispatcher = RunDispatcher(settings=resolved, database=database, engine=engine)
    interactive_browsers = BrowserSessionPool(
        lambda key: _build_interactive_browser(resolved, database, key),
    )
    return Runtime(
        settings=resolved,
        database=database,
        playwright=playwright,
        browser=browser,
        engine=engine,
        skills=skills,
        dispatcher=dispatcher,
        identity=identity,
        bearer_auth=bearer_auth,
        interactive_browsers=interactive_browsers,
    )


def _build_interactive_browser(
    settings: Settings,
    database: Database,
    session_key: str,
) -> BrowserController:
    # Playwright MCP can write downloads and other automatically named artifacts to its
    # output directory. Give each interactive session a separate, non-identifying path so
    # subprocesses cannot collide on or reference another session's artifacts.
    directory_key = hashlib.sha256(session_key.encode("utf-8")).hexdigest()
    session_settings = settings.model_copy(
        update={
            "playwright_output_dir": settings.playwright_output_dir
            / "interactive-sessions"
            / directory_key
        }
    )
    return BrowserController(PlaywrightMCPClient(session_settings), database)


@asynccontextmanager
async def runtime_lifespan(settings: Settings | None = None) -> AsyncIterator[Runtime]:
    runtime = build_runtime(settings)
    await runtime.start()
    try:
        yield runtime
    finally:
        await runtime.close()
