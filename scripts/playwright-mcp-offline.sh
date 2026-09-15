#!/bin/sh
set -eu

expected_package='@playwright/mcp@0.0.81'
browser_executable="${SKILLWRIGHT_PLAYWRIGHT_EXECUTABLE:-/usr/local/bin/skillwright-chromium}"
mcp_binary="${SKILLWRIGHT_PLAYWRIGHT_MCP_BINARY:-/usr/local/bin/playwright-mcp}"

if [ "${1:-}" != "--yes" ] || [ "${2:-}" != "$expected_package" ]; then
    echo "Skillwright image only runs the preinstalled $expected_package package" >&2
    exit 64
fi

if [ ! -x "$browser_executable" ]; then
    echo "Skillwright Chromium executable is unavailable: $browser_executable" >&2
    exit 69
fi

if [ ! -x "$mcp_binary" ]; then
    echo "Skillwright Playwright MCP executable is unavailable: $mcp_binary" >&2
    exit 69
fi

shift 2
if [ "${SKILLWRIGHT_PLAYWRIGHT_BROWSER_SANDBOX:-0}" = "1" ]; then
    exec "$mcp_binary" --executable-path "$browser_executable" "$@"
fi

exec "$mcp_binary" --executable-path "$browser_executable" --no-sandbox "$@"
