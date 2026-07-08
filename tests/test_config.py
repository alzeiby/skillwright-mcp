from __future__ import annotations

from pathlib import Path

import pytest

from skillwright_mcp.config import Settings, load_settings


def test_default_store_is_local_sqlite_under_data_dir(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "skillwright")

    expected = (tmp_path / "skillwright" / "skillwright.db").resolve()
    assert settings.resolved_database_path() == expected


def test_explicit_database_and_playwright_paths_are_resolved(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        database_path=tmp_path / "custom.db",
        playwright_output_dir=tmp_path / "browser-output",
    )

    assert settings.resolved_database_path() == (tmp_path / "custom.db").resolve()
    assert settings.resolved_playwright_output_dir() == (tmp_path / "browser-output").resolve()


def test_default_npx_command_pins_official_playwright_mcp(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)

    args = settings.playwright_args()
    assert args[:3] == ["--yes", "@playwright/mcp@0.0.81", "--caps=testing"]
    assert "--headless" in args
    assert "--isolated" in args


def test_direct_playwright_mcp_binary_does_not_receive_npx_arguments(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, playwright_command="playwright-mcp")

    args = settings.playwright_args()
    assert "--yes" not in args
    assert "@playwright/mcp@0.0.81" not in args
    assert args[0] == "--caps=testing"


def test_settings_load_from_skillwright_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SKILLWRIGHT_DATA_DIR", str(tmp_path / "env-data"))
    monkeypatch.setenv("SKILLWRIGHT_PLAYWRIGHT_HEADLESS", "false")
    monkeypatch.setenv("SKILLWRIGHT_PLAYWRIGHT_TIMEOUT_ACTION_MS", "4500")

    settings = load_settings()

    assert settings.data_dir == tmp_path / "env-data"
    assert settings.playwright_headless is False
    assert settings.playwright_timeout_action_ms == 4500
