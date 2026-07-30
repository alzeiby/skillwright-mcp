# Contributing

## Setup

Skillwright requires Python 3.11+ and Node.js/npm.

```bash
git clone https://github.com/alzeiby/skillwright-mcp.git
cd skillwright-mcp
uv sync --all-extras
```

The default browser backend is Microsoft's Playwright MCP. The real-browser integration test also needs a supported browser installed for Playwright.

## Checks

Run these before opening a pull request:

```bash
uv run ruff check src tests
uv run mypy src/skillwright_mcp
uv run pytest -q
uv build
```

The GitHub Actions workflow runs the unit suite on Python 3.11, 3.12, 3.13, and 3.14 and runs the browser integration test separately.

## Scope

Keep changes consistent with the local-first design:

- stdio MCP server
- SQLite persistence
- Microsoft's Playwright MCP as the browser backend
- generated skills exposed as first-class MCP tools
- immutable workflow versions
- deterministic replay with agent-driven repair
- environment-backed secrets

Avoid adding service infrastructure or a second control plane when the same behavior can live in the MCP process.

## Pull requests

Keep pull requests focused. Include tests for behavior changes and update the relevant file in `docs/` when public behavior or configuration changes.

Bug fixes should include a regression test when practical.
