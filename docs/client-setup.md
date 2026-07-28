# Client setup

Skillwright is a local stdio MCP server. Install `skillwright-mcp` first, then point the client at that command.

## Codex

```bash
codex mcp add skillwright -- skillwright-mcp
```

## Claude Code

```bash
claude mcp add skillwright -- skillwright-mcp
```

## GitHub Copilot CLI

```bash
copilot mcp add skillwright -- skillwright-mcp
```

Copilot CLI stores user-level MCP configuration in `~/.copilot/mcp-config.json`.

## VS Code

`skillwright-mcp` must already be installed and available on `PATH`.

[<img src="https://img.shields.io/badge/VS_Code-VS_Code?style=flat-square&label=Install%20Server&color=0098FF" alt="Install in VS Code">](https://insiders.vscode.dev/redirect?url=vscode%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522skillwright%2522%252C%2522command%2522%253A%2522skillwright-mcp%2522%257D)
[<img src="https://img.shields.io/badge/VS_Code_Insiders-VS_Code_Insiders?style=flat-square&label=Install%20Server&color=24bfa5" alt="Install in VS Code Insiders">](https://insiders.vscode.dev/redirect?url=vscode-insiders%3Amcp%2Finstall%3F%257B%2522name%2522%253A%2522skillwright%2522%252C%2522command%2522%253A%2522skillwright-mcp%2522%257D)

The same configuration can be installed from the VS Code CLI:

```bash
code --add-mcp '{"name":"skillwright","command":"skillwright-mcp"}'
```

Manual workspace configuration:

In `.vscode/mcp.json` or your user MCP configuration:

```json
{
  "servers": {
    "skillwright": {
      "type": "stdio",
      "command": "skillwright-mcp"
    }
  }
}
```

See the [VS Code MCP documentation](https://code.visualstudio.com/docs/agent-customization/mcp-servers) for user-profile installation and server management.

## Cursor

Add Skillwright to `mcp.json`:

```json
{
  "mcpServers": {
    "skillwright": {
      "command": "skillwright-mcp"
    }
  }
}
```

See the [Cursor MCP documentation](https://cursor.com/docs/mcp).

## OpenCode

CLI:

```bash
opencode mcp add skillwright -- skillwright-mcp
```

Or configure it explicitly:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "skillwright": {
        "type": "local",
        "command": ["skillwright-mcp"],
        "protocol": "auto",
        "codemode": false
      }
    }
  }
}
```

See the [OpenCode MCP documentation](https://opencode.ai/docs/mcp-servers).

## Generic stdio clients

Clients using the common `mcpServers` shape can launch:

```json
{
  "mcpServers": {
    "skillwright": {
      "command": "skillwright-mcp",
      "args": []
    }
  }
}
```

The exact outer configuration key is client-specific. The server process itself is always `skillwright-mcp` over stdio.

## Run from a checkout

For development without a global tool install:

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

## Tool list refresh

Saved skills are registered dynamically as `skillwright_*` tools. Skillwright publishes the standard MCP tool-list change notification when a skill changes.

In VS Code, run **MCP: Reset Cached Tools** if a newly generated skill does not appear. Other clients may require their MCP refresh command or a server reconnect.

[Back to docs](README.md)
