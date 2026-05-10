"""
kairix brief — session briefing synthesis.

Usage:
  kairix brief <agent> [--print] [--memory-root PATH]

Generates a session briefing at the configured briefing dir and prints
the path and first 30 lines to stdout.

Adapter only — business logic lives in
``kairix.use_cases.brief.run_brief``.
"""

from __future__ import annotations

import argparse
import sys

from kairix.use_cases.brief import BriefOutput, run_brief


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kairix brief",
        description="Generate a session briefing for an agent.",
    )
    parser.add_argument(
        "agent",
        help="Agent name (builder, shape, growth, consultant).",
    )
    parser.add_argument(
        "--print",
        dest="print_output",
        action="store_true",
        default=False,
        help="Print the full briefing to stdout.",
    )
    parser.add_argument(
        "--memory-root",
        dest="memory_root",
        default=None,
        help="Root directory containing agent subdirectories (e.g. /path/to/04-Agent-Knowledge).",
    )
    return parser


def format_output(out: BriefOutput, *, print_full: bool) -> str:
    """Render the operator-facing stdout — preview or full content."""
    if not out.content:
        return ""
    if print_full:
        return out.content
    lines = out.content.splitlines()
    preview = "\n".join(lines[:30])
    if len(lines) > 30:
        preview = f"{preview}\n\n... ({len(lines) - 30} more lines — see {out.path})"
    return preview


def main(args: list[str] | None = None) -> None:
    """Entry point for ``kairix brief``."""
    if args is None:
        args = sys.argv[2:]  # strip 'kairix brief'
    parsed = build_parser().parse_args(args)

    if parsed.memory_root:
        import os

        os.environ["KAIRIX_AGENT_MEMORY_ROOT"] = parsed.memory_root

    print(f"Generating briefing for agent: {parsed.agent} ...", file=sys.stderr)
    out = run_brief(parsed.agent)

    if out.error:
        print(f"Error generating briefing: {out.error}", file=sys.stderr)
        sys.exit(1)

    if out.path:
        print(f"Briefing written to: {out.path}", file=sys.stderr)

    rendered = format_output(out, print_full=parsed.print_output)
    if rendered:
        print(rendered)  # lgtm[py/clear-text-logging-sensitive-data] — intentional CLI output of user's own briefing
