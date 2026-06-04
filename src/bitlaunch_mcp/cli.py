"""Entry point: stdio for Claude Code/Desktop, http for Hermes/remote agents."""
import argparse

from .server import mcp


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bitlaunch-mcp",
        description="MCP server for renting GPU machines via BitLaunch/Vultr",
    )
    p.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    return p


def main() -> None:
    args = build_parser().parse_args()
    if args.transport == "http":
        mcp.run(transport="http", host=args.host, port=args.port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
