# Configuration

Skillwright reads configuration from `SKILLWRIGHT_*` environment variables.

| Variable | Default | Description |
| --- | --- | --- |
| `SKILLWRIGHT_DATA_DIR` | `~/.skillwright` | Local data directory |
| `SKILLWRIGHT_DATABASE_PATH` | `<data dir>/skillwright.db` | SQLite database path |
| `SKILLWRIGHT_PLAYWRIGHT_COMMAND` | `npx` | Playwright MCP launcher command |
| `SKILLWRIGHT_PLAYWRIGHT_HEADLESS` | `true` | Run the browser headless |
| `SKILLWRIGHT_PLAYWRIGHT_OUTPUT_DIR` | `<data dir>/playwright-output` | Playwright output directory |
| `SKILLWRIGHT_PLAYWRIGHT_TIMEOUT_ACTION_MS` | `7500` | Browser action timeout |
| `SKILLWRIGHT_PLAYWRIGHT_TIMEOUT_NAVIGATION_MS` | `60000` | Navigation timeout |

## Playwright MCP command

When `SKILLWRIGHT_PLAYWRIGHT_COMMAND` is `npx` or `npx.cmd`, Skillwright launches:

```text
npx --yes @playwright/mcp@0.0.81 ...
```

Skillwright adds its required Playwright MCP arguments, including testing capabilities, isolated browser state, output directory, and configured timeouts.

If `SKILLWRIGHT_PLAYWRIGHT_COMMAND` points to another executable, Skillwright treats it as the Playwright MCP launcher and does not prepend the npm package name.

## Headed mode

Set:

```bash
export SKILLWRIGHT_PLAYWRIGHT_HEADLESS=false
```

On Windows PowerShell:

```powershell
$env:SKILLWRIGHT_PLAYWRIGHT_HEADLESS = "false"
```

## Custom data location

```bash
export SKILLWRIGHT_DATA_DIR=/path/to/skillwright-data
```

Or set the database and Playwright output paths independently:

```bash
export SKILLWRIGHT_DATABASE_PATH=/path/to/skillwright.db
export SKILLWRIGHT_PLAYWRIGHT_OUTPUT_DIR=/path/to/playwright-output
```

## Secrets

Workflow secret references use a separate namespace:

```text
SKILLWRIGHT_SECRET_<REF>
```

For example, a workflow binding with `secret_ref="LOGIN_PASSWORD"` reads `SKILLWRIGHT_SECRET_LOGIN_PASSWORD`.

[Back to docs](README.md)
