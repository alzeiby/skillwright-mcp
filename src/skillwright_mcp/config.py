from __future__ import annotations

import ssl
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_prefix="SKILLWRIGHT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+asyncpg://skillwright:skillwright@127.0.0.1:54329/skillwright"
    database_host: str | None = None
    database_port: int = Field(default=5432, ge=1, le=65_535)
    database_name: str = "skillwright"
    database_user: str = "skillwright"
    database_password: SecretStr | None = None
    database_ssl: Literal["disable", "prefer", "require", "verify-full"] | None = None
    database_ssl_root_cert: Path | None = None
    migration_runtime_database_user: str | None = None
    migration_runtime_database_secret_arn: str | None = None
    migration_marker_parameter: str | None = None
    release_image_tag: str | None = None
    database_auto_create_schema: bool = False
    allow_unauthenticated_local: bool = True
    local_principal: str = "local"
    local_role: Literal["admin", "developer", "viewer"] = "admin"
    bootstrap_admin_principal: str | None = None
    auth_token_hashes: dict[str, str] = Field(default_factory=dict)
    auth_issuer_url: str = "https://skillwright.local"
    mcp_resource_server_url: str | None = None
    aws_region: str | None = None
    aws_secret_resolution_timeout_seconds: float = Field(default=15.0, gt=0, le=20)
    execution_backend: Literal["inline", "redis"] = "redis"
    redis_url: str = "redis://127.0.0.1:63799/0"
    redis_queue_name: str = "skillwright-runs"
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8767, ge=1, le=65_535)
    healthcheck_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    worker_concurrency: int = Field(default=2, ge=1, le=32)
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
    otel_enabled: bool = False
    otel_service_name: str = "skillwright-mcp"
    otel_exporter_otlp_endpoint: str = "http://127.0.0.1:4318"

    def resolved_database_url(self) -> str:
        if self.database_host is None:
            return self.database_url
        if self.database_password is None:
            raise ValueError("database_password is required when database_host is configured")
        return URL.create(
            "postgresql+asyncpg",
            username=self.database_user,
            password=self.database_password.get_secret_value(),
            host=self.database_host,
            port=self.database_port,
            database=self.database_name,
        ).render_as_string(hide_password=False)

    def resolved_database_connect_args(self) -> dict[str, Any]:
        if self.database_host is None or self.database_ssl is None:
            return {}
        if self.database_ssl != "verify-full":
            return {"ssl": self.database_ssl}
        if self.database_ssl_root_cert is None:
            raise ValueError("database_ssl_root_cert is required for verify-full database SSL")
        context = ssl.create_default_context(cafile=str(self.database_ssl_root_cert))
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        return {"ssl": context}

    def playwright_args(self) -> list[str]:
        args = ["--yes", self.playwright_package, "--caps=testing"]
        if self.playwright_headless:
            args.append("--headless")
        if self.playwright_isolated:
            args.append("--isolated")
        args.append("--snapshot-mode=none")
        args.extend(
            [
                f"--output-dir={self.playwright_output_dir}",
                f"--timeout-action={self.playwright_timeout_action_ms}",
                f"--timeout-navigation={self.playwright_timeout_navigation_ms}",
            ]
        )
        return args
