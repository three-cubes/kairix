"""kairix entities CLI — entity management commands."""

from __future__ import annotations

import argparse
import sys
from typing import Any


def cmd_suggest(
    args: argparse.Namespace,
    *,
    deps: Any = None,
) -> int:
    """kairix entity suggest <text> — NER-based entity suggestions.

    Thin adapter around ``kairix.use_cases.entity.run_entity_suggest``.
    Reads --file when provided; otherwise uses positional text.
    """
    from kairix.use_cases.entity import run_entity_suggest

    text = args.text
    if args.file:
        from pathlib import Path

        try:
            text = Path(args.file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    out = run_entity_suggest(text, deps=deps)
    if out.error:
        print(f"ERROR: {out.error}", file=sys.stderr)
        return 1

    print(format_suggest_output(out, fmt=args.format))
    return 0


def format_suggest_output(out: Any, *, fmt: str) -> str:
    """Render an EntitySuggestOutput as table-style or jsonl text."""
    import json as _json

    if fmt == "jsonl":
        return "\n".join(
            _json.dumps(
                {
                    "text": h.text,
                    "label": h.label,
                    "is_new": h.is_new,
                    "existing_id": h.existing_id,
                    "existing_name": h.existing_name,
                    "context": h.context,
                }
            )
            for h in out.suggestions
        )
    # Table
    lines: list[str] = []
    if out.suggestions:
        lines.append(f"{'TEXT':<30} {'LABEL':<10} {'STATUS':<10} EXISTING ID/NAME")
        lines.append("-" * 80)
        for h in out.suggestions:
            status = "new" if h.is_new else "existing"
            existing = f"{h.existing_id or ''} {h.existing_name or ''}".strip()
            lines.append(f"{h.text:<30} {h.label:<10} {status:<10} {existing}")
    lines.append("")
    lines.append(f"Total: {len(out.suggestions)} entities found ({out.new_count} new, {out.existing_count} existing)")
    return "\n".join(lines)


def cmd_validate(
    args: argparse.Namespace,
    *,
    deps: Any = None,
) -> int:
    """kairix entity validate <name> — validate entity against Wikidata."""
    import json as _json

    from kairix.use_cases.entity import run_entity_validate

    out = run_entity_validate(args.name, update=args.update, deps=deps)
    if out.error:
        print(f"ERROR: Validation failed: {out.error}", file=sys.stderr)
        return 1

    if args.format == "json":
        print(_json.dumps(_validate_envelope(out), indent=2))
        return 0

    print(format_validate_table(out, with_update_hint=not args.update))
    return 0 if out.matches else 1


def _validate_envelope(out: Any) -> dict[str, Any]:
    """Mirror the on-disk envelope shape so --json output stays compatible."""
    return {
        "name": out.name,
        "neo4j_id": out.neo4j_id or None,
        "matches": [
            {
                "qid": m.qid,
                "label": m.label,
                "description": m.description,
                "url": m.url,
                "confidence": m.confidence,
            }
            for m in out.matches
        ],
        "updated": out.updated,
        "error": out.error,
    }


def format_validate_table(out: Any, *, with_update_hint: bool) -> str:
    """Render an EntityValidateOutput as the operator-facing table view."""
    lines: list[str] = []
    lines.append("")
    lines.append(f"Entity: {out.name}")
    lines.append(f"Neo4j id: {out.neo4j_id or '(not found)'}")
    if out.updated:
        lines.append("Updated: wikidata_qid written to Neo4j node")
    lines.append("")

    if not out.matches:
        lines.append("No Wikidata matches found.")
        return "\n".join(lines)

    lines.append(f"{'QID':<12} {'CONFIDENCE':<12} {'LABEL':<30} DESCRIPTION")
    lines.append("-" * 90)
    for m in out.matches:
        lines.append(f"{m.qid:<12} {m.confidence:<12} {m.label:<30} {m.description[:35]}")

    lines.append("")
    lines.append(f"Best match: {out.matches[0].url}")
    if with_update_hint and out.matches and out.matches[0].confidence in ("high", "medium"):
        lines.append("Run with --update to write wikidata_qid to Neo4j.")
    return "\n".join(lines)


def cmd_seed(args: argparse.Namespace, *, db_path: Any = None, neo4j_client: Any = None) -> int:
    """kairix entity seed — discover entities from indexed documents and seed Neo4j.

    ``db_path`` and ``neo4j_client`` are DI seams for tests: passing them
    avoids touching the global ``KAIRIX_DB_PATH`` env var or constructing
    a real Neo4j client.
    """
    import sqlite3
    from pathlib import Path

    from kairix.core.db import open_db
    from kairix.knowledge.entities.seed import scan_for_entities, seed_graph

    if db_path is None:
        from kairix.core.db import get_db_path

        db_path = Path(get_db_path())
    else:
        db_path = Path(str(db_path))
    if not db_path.exists():
        print("ERROR: kairix index not found. Run 'kairix embed' first.", file=sys.stderr)
        return 1

    db = open_db(db_path)
    try:
        candidates = scan_for_entities(db, limit=args.limit)
    except sqlite3.OperationalError as exc:
        # Index file exists but isn't populated — same operator remediation.
        print(
            f"ERROR: kairix index not found or unpopulated ({exc}). Run 'kairix embed' first.",
            file=sys.stderr,
        )
        db.close()
        return 1
    db.close()

    if not candidates:
        print("No entity candidates found in indexed documents.")
        return 0

    print(f"Found {len(candidates)} entity candidates:")
    for c in candidates[:20]:
        print(f"  [{c.entity_type:13s}] {c.name} (confidence: {c.confidence:.2f}, docs: {len(c.source_docs)})")
    if len(candidates) > 20:
        print(f"  ... and {len(candidates) - 20} more")

    if args.dry_run:
        print("\nDry run — no changes made. Remove --dry-run to seed Neo4j.")
        return 0

    if neo4j_client is None:
        from kairix.knowledge.graph.client import get_client

        neo4j: Any = get_client()
    else:
        neo4j = neo4j_client
    if not neo4j.available:
        print("ERROR: Neo4j not available. Check connection settings.", file=sys.stderr)
        return 1

    count = seed_graph(neo4j, candidates)
    print(f"\nSeeded {count}/{len(candidates)} entities into Neo4j.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kairix entity",
        description="Entity management commands",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # suggest subcommand
    p_suggest = sub.add_parser("suggest", help="Suggest new entities using NER")
    p_suggest.add_argument("text", nargs="?", default="", help="Text to analyse (or use --file)")
    p_suggest.add_argument("--file", "-f", default=None, help="Read text from file")
    p_suggest.add_argument(
        "--format",
        choices=["table", "jsonl"],
        default="table",
        help="Output format (default: table)",
    )
    p_suggest.set_defaults(func=cmd_suggest)

    # validate subcommand
    p_validate = sub.add_parser("validate", help="Validate entity against Wikidata")
    p_validate.add_argument("name", help="Entity name to look up")
    p_validate.add_argument("--update", action="store_true", help="Write wikidata_qid to Neo4j node")
    p_validate.add_argument("--format", choices=["table", "json"], default="table")
    p_validate.set_defaults(func=cmd_validate)

    # seed subcommand
    p_seed = sub.add_parser("seed", help="Discover entities from indexed documents and seed Neo4j")
    p_seed.add_argument("--limit", type=int, default=500, help="Max entities to discover (default: 500)")
    p_seed.add_argument("--dry-run", action="store_true", help="Show candidates without seeding")
    p_seed.set_defaults(func=cmd_seed)

    return parser


def main(argv: list[str] | None = None, *, db_path: Any = None, neo4j_client: Any = None) -> int:
    """Entry point for `kairix entity`.

    ``db_path`` and ``neo4j_client`` are DI seams for tests; production
    callers leave them ``None`` and the CLI resolves them from the
    environment.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "seed":
        return cmd_seed(args, db_path=db_path, neo4j_client=neo4j_client)
    if args.command == "suggest":
        return cmd_suggest(args)
    if args.command == "validate":
        return cmd_validate(args)
    return 1  # pragma: no cover — unreachable; subparsers required=True
