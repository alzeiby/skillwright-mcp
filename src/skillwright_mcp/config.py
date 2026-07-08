from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

_PLAYWRIGHT_MCP_PACKAGE = "@playwright/mcp@0.0.81"


def _default_data_dir() -> Path:
    return Path.home() / ".skillwright"


class Settings(BaseModel):
    """Validated local Skillwright configuration."""

    model_config = ConfigDict(extra="forbid")

    data_dir: Path = Field(default_factory=_default_data_dir)
    database_path: Path | None = None
    playwright_command: str = "npx"
    playwright_headless: bool = True
    playwright_output_dir: Path | None = None
    playwright_timeout_action_ms: int = Field(default=7_500, ge=1_000)
    playwright_timeout_navigation_ms: int = Field(default=60_000, ge=1_000)

    def resolved_database_path(self) -> Path:
        return (self.database_path or (self.data_dir / "skillwright.db")).expanduser().resolve()

    def resolved_playwright_output_dir(self) -> Path:
        return (
            (self.playwright_output_dir or (self.data_dir / "playwright-output"))
            .expanduser()
            .resolve()
        )

    def playwright_args(self) -> list[str]:
        command_name = Path(self.playwright_command).name.lower()
        args = ["--yes", _PLAYWRIGHT_MCP_PACKAGE] if command_name in {"npx", "npx.cmd"} else []
        args.append("--caps=testing")
        if self.playwright_headless:
            args.append("--headless")
        args.append("--isolated")
        args.append("--snapshot-mode=none")
        args.extend(
            [
                f"--output-dir={self.resolved_playwright_output_dir()}",
                f"--timeout-action={self.playwright_timeout_action_ms}",
                f"--timeout-navigation={self.playwright_timeout_navigation_ms}",
            ]
        )
        return args


def load_settings() -> Settings:
    """Load the small public configuration surface from ``SKILLWRIGHT_*`` variables."""

    values: dict[str, str] = {}
    for field_name in Settings.model_fields:
        env_name = f"SKILLWRIGHT_{field_name.upper()}"
        value = os.environ.get(env_name)
        if value is not None:
            values[field_name] = value
    return Settings.model_validate(values)
