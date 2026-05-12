"""
kairix prep — tiered L0/L1 context summary.

Usage:
  kairix prep <query> [--tier l0|l1] [--agent AGENT] [--scope SCOPE] [--json]

Adapter only — business logic lives in
``kairix.use_cases.prep.run_prep``.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Literal

from kairix.core.search.scope import Scope
from kairix.use_cases.prep import PrepDeps, PrepOutput, prep_output_to_envelope, run_prep


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kairix prep",
        description="Tiered L0/L1 context summary for a topic, grounded in retrieved documents.",
    )
    parser.add_argument("query", help="Topic to summarise")
    parser.add_argument(
        "--tier",
        choices=["l0", "l1"],
        default="l0",
        help="l0 = 2-3 sentences (default), l1 = structured overview",
    )
    parser.add_argument("--agent", default=None, help="Agent name for collection scoping")
    parser.add_argument(
        "--scope",
        default="shared+agent",
        choices=["shared", "agent", "shared+agent", "all-agents", "everything"],
        help="Collection scope (default: shared+agent)",
    )
    parser.add_argument("--json", dest="as_json", action="store_true", help="Output JSON envelope")
    return parser


def format_text(out: PrepOutput) -> str:
    """Render a ``PrepOutput`` as the human-readable text the CLI prints."""
    if out.error:
        return f"error: {out.error}"
    lines: list[str] = [
        f"Query: {out.query}",
        f"Tier:  {out.tier}",
        "",
        out.summary,
    ]
    if out.sources:
        lines.append("")
        lines.append("Sources:")
        for src in out.sources:
            lines.append(f"  - {src}")
    return "\n".join(lines)


def main(argv: list[str] | None = None, *, deps: PrepDeps | None = None) -> int:
    """Entry point for ``kairix prep``."""
    args = build_parser().parse_args(argv)
    out = run_prep(
        args.query,
        agent=args.agent,
        scope=Scope.parse(args.scope),
        tier=_as_tier(args.tier),
        deps=deps,
    )

    if args.as_json:
        print(json.dumps(prep_output_to_envelope(out), indent=2))
    else:
        print(format_text(out))

    return 1 if out.error else 0


def _as_tier(value: str) -> Literal["l0", "l1"]:
    """Narrow argparse's ``str`` to the ``Literal`` the use case expects."""
    if value == "l1":
        return "l1"
    return "l0"


if __name__ == "__main__":
    sys.exit(main())
