from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import mcp.types as mcp_types
from mcp import Client, StdioServerParameters

from .config import Settings


@dataclass(slots=True)
class BrowserResult:
    tool_name: str
    ok: bool
    text: str
    structured_content: dict[str, Any] | None
    raw: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "tool": self.tool_name,
            "text": self.text,
            "structured_content": self.structured_content,
        }


class PlaywrightMCPClient:
    """Long-lived MCP client connected to the official Playwright MCP subprocess."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Client | None = None
        self._tool_names: set[str] = set()

    async def connect(self) -> None:
        if self._client is not None:
            return
        self._settings.playwright_output_dir.mkdir(parents=True, exist_ok=True)
        parameters = StdioServerParameters(
            command=self._settings.playwright_command,
            args=self._settings.playwright_args(),
        )
        client = Client(parameters)
        await client.__aenter__()
        self._client = client
        listed = await client.list_tools()
        self._tool_names = {tool.name for tool in listed.tools}

    async def close(self) -> None:
        if self._client is None:
            return
        client, self._client = self._client, None
        self._tool_names.clear()
        await client.__aexit__(None, None, None)

    async def has_tool(self, tool_name: str) -> bool:
        await self.connect()
        return tool_name in self._tool_names

    async def call(self, tool_name: str, arguments: dict[str, Any] | None = None) -> BrowserResult:
        await self.connect()
        assert self._client is not None
        result = await self._client.call_tool(tool_name, arguments or {})
        text_parts = [
            item.text for item in result.content if isinstance(item, mcp_types.TextContent)
        ]
        raw = result.model_dump(mode="json", by_alias=True, exclude_none=True)
        structured = result.structured_content
        return BrowserResult(
            tool_name=tool_name,
            ok=not bool(result.is_error),
            text="\n".join(text_parts),
            structured_content=structured if isinstance(structured, dict) else None,
            raw=raw,
        )
