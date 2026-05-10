"""
kairix.knowledge.contradict.cli — CLI entry point for contradiction detection.

Usage:
    kairix contradict check "<new content>" [--top-k 5] [--threshold 0.45] [--top-claims 3] [--format text|json] [--agent shared]

Adapter only — business logic lives in
``kairix.use_cases.contradict.run_contradict``.
"""

from __future__ import annotations

import argparse
import json
import sys

from kairix.core.search.scope import Scope
from kairix.use_cases.contradict import ContradictOutput, run_contradict


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kairix contradict",
        description="Check whether new content contradicts existing vault knowledge",
    )
    sub = parser.add_subparsers(dest="subcommand")

    check_p = sub.add_parser("check", help="Check new content for contradictions")
    check_p.add_argument("content", help="New content to check (raw text or claim)")
    check_p.add_argument("--top-k", type=int, default=5, help="Documents to compare against per claim (default 5)")
    check_p.add_argument(
        "--threshold",
        type=float,
        default=0.45,
        help="Minimum contradiction score 0-1 (default 0.45 — calibrated for the 3-category composite scorer)",
    )
    check_p.add_argument(
        "--top-claims",
        type=int,
        default=3,
        help="Top-N high-signal claims extracted from content (default 3)",
    )
    check_p.add_argument("--format", choices=["text", "json"], default="text", help="Output format")
    check_p.add_argument("--agent", default="shared", help="Agent scope for search (default shared)")
    return parser


def format_text(out: ContradictOutput, top_k: int, threshold: float) -> str:
    """Render a ``ContradictOutput`` as the human-readable text the CLI prints."""
    if out.error:
        return f"error: {out.error}"
    if not out.contradictions:
        return f"No contradictions found (top_k={top_k}, threshold={threshold})"

    lines: list[str] = [f"⚠ {len(out.contradictions)} contradiction(s) found:", ""]
    for h in out.contradictions:
        lines.append(f"  Category: {h.category}  Score: {h.score:.2f}  Path: {h.path}")
        lines.append(f"  Reason: {h.reason}")
        lines.append(f"  Snippet: {h.snippet[:120]}...")
        lines.append("")
    return "\n".join(lines)


def to_json_envelope(out: ContradictOutput) -> list[dict]:
    """Render the JSON output the CLI emits with ``--format json``.

    Note: the CLI emits a JSON *array* (legacy shape preserved); the MCP
    envelope wraps the array in a ``contradictions`` key plus
    ``has_contradictions`` and ``error``. Both surfaces project the
    same ``ContradictionHit`` dataclass.
    """
    return [
        {
            "doc_path": h.path,
            "score": round(h.score, 4),
            "reason": h.reason,
            "snippet": h.snippet,
            "category": h.category,
            "claim": h.claim,
        }
        for h in out.contradictions
    ]


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.subcommand != "check":
        parser.print_help()
        sys.exit(1)

    out = run_contradict(
        args.content,
        agent=args.agent,
        scope=Scope.SHARED_AGENT,
        top_k=args.top_k,
        threshold=args.threshold,
        top_claims=args.top_claims,
    )

    if args.format == "json":
        print(json.dumps(to_json_envelope(out), indent=2))
    else:
        print(format_text(out, top_k=args.top_k, threshold=args.threshold))

    sys.exit(1 if out.error else 0)
