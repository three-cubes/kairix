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
``main()`` is a thin orchestrator; all rendering is in pure helpers below so
unit tests don't need to capture stdout or stub the use case.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from kairix.use_cases.timeline import TimelineResult


def build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser used by ``main``. Pure factory — exposed
    for unit tests that want to drive argument parsing without invoking I/O.
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
    return parser


def parse_iso_or_die(value: str | None, flag_name: str) -> date | None:
    """Parse an ISO date or print an error + sys.exit(1) on failure.

    Pure-ish helper: side-effect is printing to stderr + exit. Tests should
    catch ``SystemExit`` to assert the exit-on-bad-input contract.
    """
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        print(f"error: invalid {flag_name} date: {value!r}", file=sys.stderr)
        sys.exit(1)


def format_header(result: TimelineResult, limit: int) -> str:
    """Render the query/window/limit banner that prefixes every CLI run."""
    lines: list[str] = [
        f"Query:    {result.original_query}",
        f"Rewritten: {result.rewritten_query}",
    ]
    if result.time_window:
        start_str = result.time_window.get("start") or "earliest"
        end_str = result.time_window.get("end") or "latest"
        lines.append(f"Window:   {start_str} → {end_str}")
    else:
        lines.append("Window:   (no date filter — showing all)")
    lines.append(f"Limit:    {limit}")
    if result.fell_back:
        lines.append("Note:     primary temporal index empty — showing search-pipeline fallback")
    return "\n".join(lines)


def format_results(result: TimelineResult) -> str:
    """Render the result list (or the empty-results notice).

    Returns a string ready to ``print``; tests assert on the rendered form.
    """
    if not result.results:
        return "No results found."

    lines: list[str] = [f"Found {len(result.results)} result(s):", ""]
    for i, hit in enumerate(result.results, 1):
        date_str = hit.date or "undated"
        type_str = hit.chunk_type or "search"
        header_line = f"[{i}] {date_str}  {type_str}  {hit.title}".rstrip()
        preview = hit.snippet.replace("\n", " ")[:200]
        if len(hit.snippet) > 200:
            preview += "…"
        lines.extend([header_line, f"     Source: {hit.path}", f"     {preview}", ""])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    """Entry point for ``kairix timeline``.

    Thin adapter: parse argv → call ``run_timeline`` → format the
    ``TimelineResult`` for stdout. CLI/MCP parity is enforced by the
    contract test in ``tests/contracts/test_cli_mcp_parity_timeline.py``.
    """
    args = build_parser().parse_args(argv if argv is not None else sys.argv[2:])

    since = parse_iso_or_die(args.since, "--since")
    until = parse_iso_or_die(args.until, "--until")
    chunk_types: list[str] | None = [args.chunk_type] if args.chunk_type != "all" else None

    from kairix.use_cases.timeline import run_timeline

    result = run_timeline(
        args.query,
        since=since,
        until=until,
        chunk_types=chunk_types,
        limit=args.limit,
    )

    print(format_header(result, args.limit))
    print()
    if result.error:
        print(f"error: {result.error}", file=sys.stderr)
        sys.exit(1)
    print(format_results(result))
