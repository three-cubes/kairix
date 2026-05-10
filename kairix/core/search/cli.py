"""
CLI entry point for `kairix search`.

Usage:
  kairix search "query" [--agent AGENT] [--scope SCOPE] [--budget N] [--limit N] [--json]

Options:
  --agent AGENT       Agent name for collection scoping (shape, builder, etc.)
  --scope SCOPE       Collection scope: shared | agent | shared+agent | all-agents | everything
  --budget N          Token budget cap (default: 3000)
  --limit N           Max results to display (default: 10)
  --json              Output raw JSON instead of formatted text
  --no-entity-card    Skip the entity-graph augmentation when the query is an entity lookup

Adapter only — business logic lives in ``kairix.use_cases.search.run_search``.
"""

from __future__ import annotations

import argparse
import json
import sys

from kairix.core.search.scope import Scope
from kairix.use_cases.search import SearchOutput, run_search


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kairix search",
        description="Hybrid BM25 + vector search over your document store.",
    )
    parser.add_argument("query", help="Search query string")
    parser.add_argument("--agent", default=None, help="Agent name for collection scoping")
    parser.add_argument(
        "--scope",
        default="shared+agent",
        choices=["shared", "agent", "shared+agent", "all-agents", "everything"],
        help="Collection scope (default: shared+agent)",
    )
    parser.add_argument("--budget", type=int, default=3000, help="Token budget (default: 3000)")
    parser.add_argument("--limit", type=int, default=10, help="Max results to display")
    parser.add_argument("--json", dest="as_json", action="store_true", help="Output raw JSON")
    parser.add_argument(
        "--no-entity-card",
        dest="include_entity_card",
        action="store_false",
        default=True,
        help="Skip the entity-graph augmentation",
    )
    return parser


def format_text(out: SearchOutput) -> str:
    """Render a ``SearchOutput`` as the human-readable text the CLI prints."""
    lines: list[str] = [f"Query: {out.query}", f"Intent: {out.intent}"]
    if out.error:
        lines.append(f"Error: {out.error}")
        return "\n".join(lines)

    diagnostics = (
        f"Results: {len(out.results)} returned "
        f"(BM25={out.bm25_count}, vec={out.vec_count}"
        + (", vec_failed=True" if out.vec_failed else "")
        + f") | {out.total_tokens} tokens | {out.latency_ms:.0f}ms"
    )
    lines.append(diagnostics)
    lines.append("")

    for i, hit in enumerate(out.results, start=1):
        title = hit.title or hit.path.split("/")[-1]
        tier = hit.tier or "search"
        lines.append(f"{i}. [{tier}] {title}")
        lines.append(f"   {hit.path}")
        if hit.snippet:
            snippet = hit.snippet[:200].replace("\n", " ")
            if len(hit.snippet) > 200:
                snippet += "…"
            lines.append(f"   {snippet}")
        lines.append(f"   score={hit.score:.4f} | collection={hit.collection}")
        lines.append("")

    if not out.results:
        lines.append("No results found.")

    return "\n".join(lines)


def to_json_envelope(out: SearchOutput) -> dict:
    """Serialise the ``SearchOutput`` to the JSON envelope the ``--json`` flag emits."""
    envelope: dict = {
        "query": out.query,
        "intent": out.intent,
        "bm25_count": out.bm25_count,
        "vec_count": out.vec_count,
        "fused_count": out.fused_count,
        "vec_failed": out.vec_failed,
        "total_tokens": out.total_tokens,
        "latency_ms": round(out.latency_ms, 1),
        "results": [
            {
                "path": h.path,
                "title": h.title,
                "collection": h.collection,
                "score": h.score,
                "tier": h.tier,
                "snippet": h.snippet,
            }
            for h in out.results
        ],
    }
    if out.error:
        envelope["error"] = out.error
    return envelope


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    out = run_search(
        args.query,
        agent=args.agent,
        scope=Scope.parse(args.scope),
        budget=args.budget,
        limit=args.limit,
        include_entity_card=args.include_entity_card,
    )

    if args.as_json:
        print(json.dumps(to_json_envelope(out), indent=2))
    else:
        print(format_text(out))

    if out.error:
        sys.exit(1)


if __name__ == "__main__":
    main()
