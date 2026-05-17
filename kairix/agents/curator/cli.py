"""
Curator agent CLI for Kairix.

Usage:
  kairix curator health [--format text|json] [--output FILE]
                        [--staleness-days N]

Exit code is always 0 — health issues are surfaced via the report, not the
exit code, so callers (cron, agents, CI) do not see spurious failures.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any


def _default_neo4j_client_factory() -> Any:
    """Production factory: defers the heavy graph-client import until call time."""
    from kairix.knowledge.graph.client import get_client

    return get_client()


def _health_cmd(
    args: argparse.Namespace,
    *,
    neo4j_client: Any = None,
    client_factory: Callable[[], Any] = _default_neo4j_client_factory,
) -> None:
    from kairix.agents.curator.health import (
        format_report_json,
        format_report_text,
        run_health_check,
    )

    if neo4j_client is None:
        neo4j_client = client_factory()

    report = run_health_check(neo4j_client, staleness_days=args.staleness_days)

    output = format_report_json(report) if args.format == "json" else format_report_text(report)

    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"Health report written to {args.output}")
    else:
        print(output, end="")

    sys.exit(0)


def main(
    argv: list[str] | None = None,
    *,
    neo4j_client: Any = None,
    client_factory: Callable[[], Any] = _default_neo4j_client_factory,
) -> None:
    """Entry point for `kairix curator` subcommand.

    The ``neo4j_client`` keyword lets BDD/integration tests inject a
    ``FakeNeo4jClient`` directly. The ``client_factory`` keyword is the
    public DI seam for unit tests that want to exercise the
    "no-injection → factory call" branch without monkey-patching the
    ``get_client`` import inside :func:`_health_cmd`.
    """
    parser = argparse.ArgumentParser(
        prog="kairix curator",
        description="Curator agent: entity graph health monitoring and enrichment.",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # --- health ---
    health_parser = subparsers.add_parser(
        "health",
        help="Run entity graph health check (CA-1)",
    )
    health_parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format: text (vault-ready Markdown) or json (default: text)",
    )
    health_parser.add_argument(
        "--output",
        default=None,
        metavar="FILE",
        help="Write report to FILE instead of stdout",
    )
    health_parser.add_argument(
        "--staleness-days",
        type=int,
        default=90,
        dest="staleness_days",
        metavar="N",
        help="Flag entities with no activity for N days as stale (default: 90)",
    )
    health_parser.set_defaults(func=_health_cmd)

    parsed = parser.parse_args(argv)
    if parsed.func is _health_cmd:
        _health_cmd(parsed, neo4j_client=neo4j_client, client_factory=client_factory)
    else:  # pragma: no cover — only one subcommand today
        parsed.func(parsed)


if __name__ == "__main__":
    main()
