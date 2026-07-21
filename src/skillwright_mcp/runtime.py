from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from uuid import uuid4

from .browser import BrowserController
from .config import Settings
from .db import Database
from .engine import WorkflowEngine
from .playwright import PlaywrightMCPClient
from .skills import SkillService


@dataclass(slots=True)
class Runtime:
    database: Database
    engine: WorkflowEngine
    skills: SkillService
    interactive_browser: BrowserController
    interactive_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def build_runtime(settings: Settings) -> Runtime:
    database = Database(settings.resolved_database_path())
    engine = WorkflowEngine(
        database,
        lambda: _build_execution_browser(settings),
    )
    return Runtime(
        database=database,
        engine=engine,
        skills=SkillService(database),
        interactive_browser=_build_interactive_browser(settings, database),
    )


def _build_interactive_browser(settings: Settings, database: Database) -> BrowserController:
    session_key = uuid4().hex
    session_settings = settings.model_copy(
        update={
            "playwright_output_dir": settings.resolved_playwright_output_dir()
            / "interactive"
            / session_key
        }
    )
    return BrowserController(
        PlaywrightMCPClient(session_settings),
        database,
        history_scope=f"interactive-{session_key}",
    )


def _build_execution_browser(settings: Settings) -> BrowserController:
    run_key = uuid4().hex
    run_settings = settings.model_copy(
        update={
            "playwright_output_dir": settings.resolved_playwright_output_dir()
            / "executions"
            / run_key
        }
    )
    return BrowserController(
        PlaywrightMCPClient(run_settings),
    )
