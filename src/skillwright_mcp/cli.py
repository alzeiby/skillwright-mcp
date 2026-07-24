from __future__ import annotations

import argparse

from .server import mcp


def main() -> None:
    parser = argparse.ArgumentParser(description="Skillwright MCP server")
    parser.parse_args()
    mcp.run()


if __name__ == "__main__":
    main()
