"""
kairix timeline — Temporal query rewriting + date-aware retrieval.

Usage:
  kairix timeline <query> [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--limit N]
  kairix timeline --help

Examples:
  kairix timeline "what was completed last week on kairix"
  kairix timeline "what happened in March 2026" --since 2026-03-01 --until 2026-03-31
  kairix timeline "recent Bower Bird changes" --limit 10

Adapter only — business logic lives in ``kairix.use_cases.timeline.run_timeline``.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date


def main(argv: list[str] | None = None) -> None:
    """Entry point for ``kairix timeline``.

    Thin adapter: parse argv → call ``run_timeline`` → format the
    ``TimelineResult`` for stdout. CLI/MCP parity is enforced by the
    contract test in ``tests/contracts/test_cli_mcp_parity_timeline.py``.
    """
    parser = argparse.ArgumentParser(
        prog="kairix timeline",
        description="Temporal query over Kanban boards and daily memory logs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  kairix timeline "what was completed last week on kairix"
  kairix timeline "what happened in March 2026" --since 2026-03-01 --until 2026-03-31
  kairix timeline "recent Bower Bird changes" --limit 10
""",
    )
    parser.add_argument("query", help="Temporal query string")
    parser.add_argument(
        "--since",
        metavar="YYYY-MM-DD",
        help="Override start date (ISO format). If omitted, extracted from query.",
    )
    parser.add_argument(
        "--until",
        metavar="YYYY-MM-DD",
        help="Override end date (ISO format). If omitted, extracted from query.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        metavar="N",
        help="Maximum number of results to return (default: 10)",
    )
    parser.add_argument(
        "--type",
        choices=["board_card", "memory_section", "all"],
        default="all",
        dest="chunk_type",
        help="Filter chunk type (default: all)",
    )

    args = parser.parse_args(argv if argv is not None else sys.argv[2:])

    # Parse explicit date overrides up-front so we can fail fast on bad input.
    since: date | None = None
    until: date | None = None
    if args.since:
        try:
            since = date.fromisoformat(args.since)
        except ValueError:
            print(f"error: invalid --since date: {args.since!r}", file=sys.stderr)
            sys.exit(1)
    if args.until:
        try:
            until = date.fromisoformat(args.until)
        except ValueError:
            print(f"error: invalid --until date: {args.until!r}", file=sys.stderr)
            sys.exit(1)

    chunk_types: list[str] | None = None
    if args.chunk_type != "all":
        chunk_types = [args.chunk_type]

    from kairix.use_cases.timeline import run_timeline

    result = run_timeline(
        args.query,
        since=since,
        until=until,
        chunk_types=chunk_types,
        limit=args.limit,
    )

    print(f"Query:    {result.original_query}")
    print(f"Rewritten: {result.rewritten_query}")
    if result.time_window:
        start_str = result.time_window.get("start") or "earliest"
        end_str = result.time_window.get("end") or "latest"
        print(f"Window:   {start_str} → {end_str}")
    else:
        print("Window:   (no date filter — showing all)")
    print(f"Limit:    {args.limit}")
    if result.fell_back:
        print("Note:     primary temporal index empty — showing search-pipeline fallback")
    print()

    if result.error:
        print(f"error: {result.error}", file=sys.stderr)
        sys.exit(1)

    if not result.results:
        print("No results found.")
        return

    print(f"Found {len(result.results)} result(s):\n")

    for i, hit in enumerate(result.results, 1):
        date_str = hit.date or "undated"
        type_str = hit.chunk_type or "search"
        print(f"[{i}] {date_str}  {type_str}  {hit.title}".rstrip())
        print(f"     Source: {hit.path}")
        preview = hit.snippet.replace("\n", " ")[:200]
        if len(hit.snippet) > 200:
            preview += "…"
        print(f"     {preview}")
        print()
