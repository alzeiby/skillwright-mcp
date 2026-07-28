# Getting started

## Requirements

- Python 3.11+
- Node.js/npm
- an MCP client that can launch a local stdio server
- a browser supported by Playwright MCP

Skillwright uses Microsoft's `@playwright/mcp@0.0.81` package by default.

## Install

```bash
git clone https://github.com/alzeiby/skillwright-mcp.git
cd skillwright-mcp
uv tool install .
```

Verify the command is available:

```bash
skillwright-mcp --help
```

## MCP client setup

Generic stdio configuration:

```json
{
  "mcpServers": {
    "skillwright": {
      "command": "skillwright-mcp"
    }
  }
}
```

Codex:

```bash
codex mcp add skillwright -- skillwright-mcp
```

Claude Code:

```bash
claude mcp add skillwright -- skillwright-mcp
```

For development from a checkout, point the client at `uv run` instead of installing the package globally:

```json
{
  "command": "uv",
  "args": [
    "run",
    "--directory",
    "/absolute/path/to/skillwright-mcp",
    "skillwright-mcp"
  ]
}
```

## Local data

Default paths:

```text
~/.skillwright/skillwright.db
~/.skillwright/playwright-output/
```

The SQLite schema is created automatically. Recognized databases from older Skillwright versions are migrated on startup. A non-empty SQLite database that is not recognized as Skillwright is rejected without modification.

## First skill

Start a recording, perform the task through the `browser_*` tools, then stop the recording:

```text
skill_record_start(name="search_docs", description="Search the docs")

browser_navigate(url="https://example.test/docs")
browser_snapshot()
browser_fill(target="e5", text="playwright", element="Search")
browser_snapshot()
browser_click(target="e9", element="Search")

skill_record_stop()
```

`skill_record_stop` compiles the successful browser actions, writes a new workflow version, and registers a generated MCP tool.

For a skill named `search_docs`, the generated name is stable and collision-resistant, for example:

```text
skillwright_search_docs_911bf176
```

Continue with [Authoring skills](authoring.md) to add typed inputs, outputs, secrets, and version changes.
