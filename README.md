# Skillwright

<div align="center">

**Turn browser tasks into reusable MCP tools.**

Record a task with Playwright MCP, save it as a versioned workflow, and call it again like any other MCP tool.

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-stdio-7C3AED?style=flat-square)](https://modelcontextprotocol.io/)
[![Playwright](https://img.shields.io/badge/Playwright-MCP-2EAD33?style=flat-square&logo=playwright&logoColor=white)](https://github.com/microsoft/playwright-mcp)
[![License](https://img.shields.io/badge/License-MIT-22C55E?style=flat-square)](LICENSE)

</div>

> **Alpha:** Skillwright is under active development. Workflow formats and authoring tools may still change.

Skillwright sits between an MCP client and Microsoft's Playwright MCP server. You use the browser normally, record the successful actions, replace literals with typed inputs, and save the result. From then on, the workflow is exposed as a generated MCP tool and replayed deterministically instead of being re-planned from scratch.

```text
browser actions  ->  recorded workflow  ->  generated MCP tool
                                              |
                                              +-> deterministic replay
```

## Install

You need Python 3.11+ and Node.js/npm.

```bash
git clone https://github.com/alzeiby/skillwright-mcp.git
cd skillwright-mcp
uv tool install .
```

Then add Skillwright to any client that can launch a stdio MCP server:

```json
{
  "mcpServers": {
    "skillwright": {
      "command": "skillwright-mcp"
    }
  }
}
```

For Codex or Claude Code:

```bash
codex mcp add skillwright -- skillwright-mcp
claude mcp add skillwright -- skillwright-mcp
```

VS Code, Cursor, Copilot CLI, OpenCode, and other clients are covered in [client setup](docs/client-setup.md).

## Record once, call it again

Start a recording and perform the task through Skillwright's `browser_*` tools:

```text
skill_record_start(name="search_docs", description="Search the docs")

browser_navigate(url="https://example.test/docs")
browser_snapshot()
browser_fill(target="e5", text="playwright", element="Search")
browser_snapshot()
browser_click(target="e9", element="Search")

skill_record_stop()
```

Turn the recorded search text into an input:

```text
skill_parameterize(
  name="search_docs",
  bindings=[{
    "step": 1,
    "field": "value",
    "input_name": "query",
    "input_type": "string"
  }]
)
```

Skillwright registers the saved workflow as a generated tool with a stable, collision-resistant name:

```text
skillwright_search_docs_911bf176(query="playwright")
```

The generated tool uses the workflow schema directly: public inputs become typed MCP parameters, secret inputs stay out of the public schema, and declared outputs become structured results.

## What it keeps track of

- **Recorded browser actions.** Navigation, clicks, fills, selects, and waits are compiled into replayable steps.
- **Targets.** Replays resolve persisted semantic evidence and durable locators instead of reusing old snapshot references.
- **Versions.** Workflow versions are immutable. Rollback creates a new version rather than rewriting history.
- **Repair.** Failed workflows return structured repair evidence; proposed fixes are replay-validated before they are saved.
- **Composition.** Skills can call pinned versions of other skills while sharing the same execution browser.
- **Secrets.** Secret values are resolved from environment-backed bindings and are not stored as workflow plaintext.

## Local by default

Skillwright is a local stdio server. It stores its state in SQLite and launches Playwright MCP for browser work.

```text
~/.skillwright/skillwright.db
~/.skillwright/playwright-output/
```

There is no required REST service, worker queue, remote database, or account system. See [Architecture](docs/architecture.md) for the runtime model and persistence details.

## Documentation

| | |
| --- | --- |
| [Getting started](docs/getting-started.md) | Installation, client setup, and your first saved skill |
| [Authoring](docs/authoring.md) | Inputs, outputs, secrets, and workflow editing |
| [Tool reference](docs/tools.md) | Browser, recording, versioning, repair, and composition tools |
| [Repair and composition](docs/repair-and-composition.md) | Recovery and reusable workflow building blocks |
| [Configuration](docs/configuration.md) | Paths, Playwright MCP, and runtime settings |
| [Architecture](docs/architecture.md) | Process model, persistence, replay, and secrets |
| [Troubleshooting](docs/troubleshooting.md) | Common setup and runtime failures |

## Development

```bash
uv sync --all-extras
uv run ruff check src tests
uv run mypy src/skillwright_mcp
uv run pytest -q
uv build
```

Contribution and project policies are in [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), and [SUPPORT.md](SUPPORT.md).

## License

[MIT](LICENSE)
