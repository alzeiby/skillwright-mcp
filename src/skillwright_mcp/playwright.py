from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import mcp.types as mcp_types
from mcp import Client, StdioServerParameters

from .config import Settings


@dataclass(slots=True)
class BrowserResult:
    ok: bool
    text: str
    structured_content: dict[str, Any] | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "text": self.text,
            "structured_content": self.structured_content,
        }


class PlaywrightMCPClient:
    """Long-lived MCP client connected to the official Playwright MCP subprocess."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Client | None = None

    async def close(self) -> None:
        if self._client is None:
            return
        client, self._client = self._client, None
        await client.__aexit__(None, None, None)

    async def call(self, tool_name: str, arguments: dict[str, Any] | None = None) -> BrowserResult:
        if self._client is None:
            self._settings.resolved_playwright_output_dir().mkdir(parents=True, exist_ok=True)
            parameters = StdioServerParameters(
                command=self._settings.playwright_command,
                args=self._settings.playwright_args(),
            )
            client = Client(parameters)
            await client.__aenter__()
            self._client = client
        result = await self._client.call_tool(tool_name, arguments or {})
        text_parts = [
            item.text for item in result.content if isinstance(item, mcp_types.TextContent)
        ]
        structured = result.structured_content
        return BrowserResult(
            ok=not bool(result.is_error),
            text="\n".join(text_parts),
            structured_content=structured if isinstance(structured, dict) else None,
        )
