"""CLI entry point.

  powerbi-mcp                 run the MCP server over stdio (what Claude Desktop/Code uses)
  powerbi-mcp --http          run over streamable HTTP (for remote / shared deployments)
  powerbi-mcp login           sign in with device code (device_code auth mode)
  powerbi-mcp logout          remove the cached sign-in
  powerbi-mcp check           test the connection and list a few workspaces
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys


def main() -> None:
    parser = argparse.ArgumentParser(prog="powerbi-mcp", description="Power BI MCP server")
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "login", "logout", "check"])
    parser.add_argument("--http", action="store_true", help="Use streamable HTTP transport instead of stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    from .auth import TokenProvider
    from .config import Settings

    if args.command == "login":
        TokenProvider(Settings.from_env()).interactive_login()
        return
    if args.command == "logout":
        TokenProvider(Settings.from_env()).logout()
        return

    from . import server

    if args.command == "check":
        result = asyncio.run(server.check_connection())
        print(json.dumps(result, indent=2))
        sys.exit(0 if result.get("ok") else 1)

    if args.http:
        server.run("streamable-http", host=args.host, port=args.port)
    else:
        server.run("stdio")


if __name__ == "__main__":
    main()
