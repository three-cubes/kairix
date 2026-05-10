"""
kairix research — iterative research with synthesis.

Usage:
  kairix research <query> [--max-turns N] [--json]

Adapter only — business logic lives in
``kairix.use_cases.research.run_research_use_case``.
"""

from __future__ import annotations

import argparse
import json
import sys

from kairix.use_cases.research import (
    ResearchDeps,
    ResearchOutput,
    research_output_to_envelope,
    run_research_use_case,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kairix research",
        description="Iterative research over the knowledge store with LLM synthesis.",
    )
    parser.add_argument("query", help="Research question")
    parser.add_argument(
        "--max-turns",
        type=int,
        default=4,
        help="Cap on search/refine cycles (default 4, clamped to 1-10)",
    )
    parser.add_argument("--json", dest="as_json", action="store_true", help="Output JSON envelope")
    return parser


def format_text(out: ResearchOutput) -> str:
    """Render a ``ResearchOutput`` as the human-readable text the CLI prints."""
    if out.error:
        return f"error: {out.error}"
    lines: list[str] = [
        f"Query:      {out.query}",
        f"Turns:      {out.turns}",
        f"Confidence: {out.confidence:.2f}",
        "",
        "Synthesis:",
        out.synthesis or "(no synthesis returned)",
    ]
    if out.gaps:
        lines.append("")
        lines.append("Gaps / open questions:")
        for g in out.gaps:
            lines.append(f"  - {g}")
    if out.retrieved_chunks:
        lines.append("")
        lines.append(f"Retrieved chunks ({len(out.retrieved_chunks)}):")
        for c in out.retrieved_chunks[:5]:
            label = c.get("path") if isinstance(c, dict) else str(c)
            lines.append(f"  - {label}")
        if len(out.retrieved_chunks) > 5:
            lines.append(f"  ... ({len(out.retrieved_chunks) - 5} more)")
    return "\n".join(lines)


def main(argv: list[str] | None = None, *, deps: ResearchDeps | None = None) -> int:
    """Entry point for ``kairix research``."""
    args = build_parser().parse_args(argv)

    out = run_research_use_case(args.query, max_turns=args.max_turns, deps=deps)

    if args.as_json:
        print(json.dumps(research_output_to_envelope(out), indent=2))
    else:
        print(format_text(out))

    return 1 if out.error else 0


if __name__ == "__main__":
    sys.exit(main())
