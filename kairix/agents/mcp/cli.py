"""
kairix.agents.mcp.cli — CLI entry point for the MCP server.

Usage:
    kairix mcp serve [--port PORT] [--transport stdio|http|sse] [--no-sse]

Transports:
    stdio — for Claude Desktop / inline use (default).
    http  — uvicorn-served streamable HTTP at /mcp (recommended for server
            deployments). Also mounts /sse for back-compat unless --no-sse.
    sse   — deprecated alias for http (kept for back-compat with existing
            scripts; emits a deprecation warning).

Requires kairix[agents]: pip install 'kairix[agents]'
"""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="kairix mcp",
        description="MCP server: expose search/entity/prep/timeline as MCP tools",
    )
    sub = parser.add_subparsers(dest="subcommand")

    serve_p = sub.add_parser("serve", help="Start the MCP server")
    serve_p.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port to listen on for http/sse transport (default: 8080)",
    )
    serve_p.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind to for http/sse transport (default: 127.0.0.1). "
        "WARNING: The MCP server has no authentication. Do not bind to 0.0.0.0 "
        "unless you have network-level access controls in place.",
    )
    serve_p.add_argument(
        "--transport",
        choices=["stdio", "http", "sse"],
        default="stdio",
        help="Transport: stdio (default, for Claude Desktop), http (streamable "
        "HTTP at /mcp + legacy /sse), or sse (deprecated alias for http)",
    )
    serve_p.add_argument(
        "--no-sse",
        action="store_true",
        help="When --transport=http, omit the legacy /sse mount and serve only /mcp",
    )

    args = parser.parse_args(argv)

    if args.subcommand == "serve":
        _cmd_serve(args)
    else:
        parser.print_help()
        sys.exit(1)


def _resolve_port(args: argparse.Namespace) -> int:
    """Resolve MCP port: CLI flag → env var → config → auto-detect."""
    import os

    # CLI flag takes precedence (argparse default is 8080)
    if "--port" in sys.argv:
        return int(args.port)

    # Environment variable
    env_port = os.environ.get("KAIRIX_MCP_PORT")
    if env_port:
        return int(env_port)

    # Auto-detect: check if default port is available
    from kairix.platform.onboard.ports import find_available_port, is_port_available

    default = 8080
    if is_port_available(default):
        return default

    suggested = find_available_port(preferred=default)
    print(
        f"Port {default} is in use — using {suggested} instead. "
        f"Set KAIRIX_MCP_PORT={suggested} to make this permanent.",
        file=sys.stderr,
    )
    return suggested


def _cmd_serve(args: argparse.Namespace) -> None:
    try:
        from kairix.agents.mcp.server import build_server
    except ImportError:
        print(
            "Error: MCP dependencies not installed. Run: pip install 'kairix[agents]'",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.transport == "sse":
        print(
            "WARNING: --transport=sse is deprecated; use --transport=http "
            "(serves both /mcp and /sse). Continuing as http.",
            file=sys.stderr,
        )
        args.transport = "http"

    if args.transport == "stdio":
        server = build_server(host=args.host, port=args.port)
        print("Starting kairix MCP server (stdio transport)", file=sys.stderr)
        server.run(transport="stdio")
        return

    # http transport — streamable HTTP at /mcp via uvicorn, optional /sse legacy
    port = _resolve_port(args)
    server = build_server(host=args.host, port=port)

    from kairix.agents.mcp.capability_probe import build_capability_probe
    from kairix.agents.mcp.readiness import EventReadinessGate
    from kairix.agents.mcp.transport import build_mcp_app

    # The http transport's lazy-init paths (Neo4j, vector index, LLM clients)
    # are exercised on first tool call rather than at startup, so the gate
    # is marked ready immediately after the app is built. When we add a real
    # warm-up phase, mark_ready() moves to the end of that phase.
    gate = EventReadinessGate()
    capability_probe = build_capability_probe()
    app = build_mcp_app(
        server,
        with_sse=not args.no_sse,
        readiness_check=gate.is_ready,
        capability_probe=capability_probe,
    )
    gate.mark_ready()

    sse_status = "+ /sse legacy" if not args.no_sse else "(no /sse)"
    print(
        f"Starting kairix MCP server on http://{args.host}:{port}/mcp {sse_status}",
        file=sys.stderr,
    )

    import uvicorn

    uvicorn.run(app, host=args.host, port=port, log_level="info")
