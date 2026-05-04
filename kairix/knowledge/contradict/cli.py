"""
kairix.knowledge.contradict.cli — CLI entry point for contradiction detection.

Usage:
    kairix contradict check "<new content>" [--top-k 5] [--threshold 0.6] [--format text|json]
"""

from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> None:
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

    args = parser.parse_args(argv)

    if args.subcommand != "check":
        parser.print_help()
        sys.exit(1)

    from kairix.knowledge.contradict.detector import check_contradiction
    from kairix.platform.llm import get_default_backend

    llm = get_default_backend()

    results = check_contradiction(
        content=args.content,
        llm=llm,
        top_k=args.top_k,
        threshold=args.threshold,
        top_claims=args.top_claims,
    )

    if args.format == "json":
        print(
            json.dumps(
                [
                    {
                        "doc_path": r.doc_path,
                        "score": round(r.score, 4),
                        "reason": r.reason,
                        "snippet": r.snippet,
                        "category": r.category,
                        "claim": r.claim,
                    }
                    for r in results
                ],
                indent=2,
            )
        )
    else:
        if not results:
            print(f"No contradictions found (top_k={args.top_k}, threshold={args.threshold})")
        else:
            print(f"⚠ {len(results)} contradiction(s) found:\n")
            for r in results:
                print(f"  Category: {r.category}  Score: {r.score:.2f}  Path: {r.doc_path}")
                print(f"  Reason: {r.reason}")
                print(f"  Snippet: {r.snippet[:120]}...")
                print()

    sys.exit(0)
