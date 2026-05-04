"""
CLI entry point for `kairix search`.

Usage:
  kairix search "query" [--agent AGENT] [--scope SCOPE] [--budget N] [--json]

Options:
  --agent AGENT   Agent name for collection scoping (shape, builder, etc.)
  --scope SCOPE   Collection scope: shared | agent | shared+agent (default: shared+agent)
  --budget N      Token budget cap (default: 3000)
  --json          Output raw JSON instead of formatted text
  --limit N       Max results to display (default: 10)

Exits 0 on success, 1 on error.
"""

import argparse
import json

from kairix.core.search.config_loader import load_config
from kairix.core.search.pipeline import SearchResult


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
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
    return parser.parse_args(argv)


def _format_result(sr: SearchResult, limit: int) -> str:
    """Format SearchResult as human-readable text."""
    lines: list[str] = []
    lines.append(f"Query: {sr.query}")
    lines.append(f"Intent: {sr.intent.value}")

    if sr.error:
        lines.append(f"Error: {sr.error}")
        return "\n".join(lines)

    lines.append(
        f"Results: {len(sr.results)} returned "
        f"(BM25={sr.bm25_count}, vec={sr.vec_count}"
        + (", vec_failed=True" if sr.vec_failed else "")
        + f") | {sr.total_tokens} tokens | {sr.latency_ms:.0f}ms"
    )
    lines.append("")

    for i, budgeted in enumerate(sr.results[:limit], start=1):
        r = budgeted.result
        title = r.title or r.path.split("/")[-1]
        lines.append(f"{i}. [{budgeted.tier}] {title}")
        lines.append(f"   {r.path}")
        if r.snippet:
            snippet = r.snippet[:200].replace("\n", " ")
            if len(r.snippet) > 200:
                snippet += "…"
            lines.append(f"   {snippet}")
        lines.append(f"   score={r.boosted_score:.4f} | collection={r.collection}")
        lines.append("")

    if not sr.results:
        lines.append("No results found.")

    return "\n".join(lines)


def main(argv: list[str] | None = None, *, pipeline=None) -> None:
    import sys

    args = _parse_args(argv)

    cfg = load_config()

    if pipeline is None:
        from kairix.core.factory import build_search_pipeline

        pipeline = build_search_pipeline(config=cfg)

    sr = pipeline.search(
        query=args.query,
        budget=args.budget,
        scope=args.scope,
        agent=args.agent,
    )

    if args.as_json:
        output: dict = {
            "query": sr.query,
            "intent": sr.intent.value,
            "bm25_count": sr.bm25_count,
            "vec_count": sr.vec_count,
            "fused_count": sr.fused_count,
            "vec_failed": sr.vec_failed,
            "total_tokens": sr.total_tokens,
            "latency_ms": round(sr.latency_ms, 1),
            "results": [
                {
                    "path": b.result.path,
                    "title": b.result.title,
                    "collection": b.result.collection,
                    "score": b.result.boosted_score,
                    "tier": b.tier,
                    "snippet": b.content[:500],
                }
                for b in sr.results[: args.limit]
            ],
        }
        if sr.error:
            output["error"] = sr.error
        print(json.dumps(output, indent=2))
        if sr.error:
            sys.exit(1)
    else:
        print(_format_result(sr, limit=args.limit))
        if sr.error:
            sys.exit(1)


if __name__ == "__main__":
    main()
