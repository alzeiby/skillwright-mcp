# Troubleshooting

## `skillwright-mcp` is not found

Confirm the tool install:

```bash
uv tool install .
skillwright-mcp --help
```

For a source checkout, configure the client to run `uv run --directory /path/to/skillwright-mcp skillwright-mcp` instead.

## Playwright MCP does not start

The default launcher is `npx` and requires Node.js/npm on `PATH`.

```bash
node --version
npx --version
```

Skillwright launches the pinned `@playwright/mcp` package automatically when `SKILLWRIGHT_PLAYWRIGHT_COMMAND` is `npx`.

For browser-launch debugging, switch to headed mode:

```bash
export SKILLWRIGHT_PLAYWRIGHT_HEADLESS=false
```

PowerShell:

```powershell
$env:SKILLWRIGHT_PLAYWRIGHT_HEADLESS = "false"
```

## A saved skill does not appear as a tool

Skillwright sends a standard MCP tool-list change notification after saving or changing a skill. Some clients cache the tool list.

In VS Code, run **MCP: Reset Cached Tools**. In other clients, refresh MCP tools or restart the Skillwright connection. `skill_list` can confirm that the skill was saved and show its generated tool name.

## A generated tool returns `repair_required`

The page no longer matched the saved workflow closely enough to continue safely. Inspect the returned failing step, target evidence, current page state, candidates, and side-effect state, then use `skill_repair`.

See [Repair and composition](repair-and-composition.md).

## A secret is not configured

For a binding such as `LOGIN_PASSWORD`, set:

```bash
export SKILLWRIGHT_SECRET_LOGIN_PASSWORD='...'
```

Use `skill_secret_status` to check whether required secret inputs have bindings.

## SQLite database is rejected

Skillwright only initializes an empty database or opens a recognized Skillwright schema. It will not reinterpret an unrelated non-empty SQLite file.

Use a different `SKILLWRIGHT_DATABASE_PATH` or move the unrelated database elsewhere.

[Back to docs](README.md)
