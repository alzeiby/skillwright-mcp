from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="SKILLWRIGHT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = (
        "postgresql+asyncpg://skillwright:skillwright@127.0.0.1:54329/skillwright"
    )
    database_auto_create_schema: bool = False
    allow_unauthenticated_local: bool = True
    local_principal: str = "local"
    local_role: Literal["admin", "developer", "viewer"] = "admin"
    execution_backend: Literal["inline", "redis"] = "redis"
    redis_url: str = "redis://127.0.0.1:63799/0"
    redis_queue_name: str = "skillwright-runs"
    worker_concurrency: int = Field(default=2, ge=1, le=32)
    worker_job_timeout_seconds: int = Field(default=300, ge=10)
    repair_wait_timeout_seconds: int = Field(default=900, ge=30)
    repair_poll_interval_seconds: float = Field(default=0.5, ge=0.1, le=10)
    approval_wait_timeout_seconds: int = Field(default=900, ge=30)
    run_stale_after_seconds: int = Field(default=600, ge=30)
    stale_reaper_interval_seconds: int = Field(default=30, ge=5)
    playwright_command: str = "npx"
    playwright_package: str = "@playwright/mcp@0.0.81"
    playwright_headless: bool = True
    playwright_isolated: bool = True
    playwright_output_dir: Path = Path("playwright-output")
    playwright_timeout_action_ms: int = Field(default=7_500, ge=1_000)
    playwright_timeout_navigation_ms: int = Field(default=60_000, ge=1_000)

    def playwright_args(self) -> list[str]:
        args = ["--yes", self.playwright_package, "--caps=testing"]
        if self.playwright_headless:
            args.append("--headless")
        if self.playwright_isolated:
            args.append("--isolated")
        args.extend(
            [
                f"--output-dir={self.playwright_output_dir}",
                f"--timeout-action={self.playwright_timeout_action_ms}",
                f"--timeout-navigation={self.playwright_timeout_navigation_ms}",
            ]
        )
        return args
