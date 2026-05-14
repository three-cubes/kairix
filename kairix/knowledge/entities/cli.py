"""kairix entities CLI — entity management commands."""

from __future__ import annotations

import argparse
import sys
from typing import Any

# F17 — argparse 'store_true' action name. Centralised so adding new
# boolean flags doesn't push the literal over the duplicated-string limit.
# Every site is "this is a boolean flag with no value"; the coupling is real.
_STORE_TRUE = "store_true"


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
    p_validate.add_argument("--update", action=_STORE_TRUE, help="Write wikidata_qid to Neo4j node")
    p_validate.add_argument("--format", choices=["table", "json"], default="table")
    p_validate.set_defaults(func=cmd_validate)

    # seed subcommand
    p_seed = sub.add_parser("seed", help="Discover entities from indexed documents and seed Neo4j")
    p_seed.add_argument("--limit", type=int, default=500, help="Max entities to discover (default: 500)")
    p_seed.add_argument("--dry-run", action=_STORE_TRUE, help="Show candidates without seeding")
    p_seed.set_defaults(func=cmd_seed)

    # get subcommand — direct entity lookup by name (Phase 3e of #168)
    p_get = sub.add_parser("get", help="Look up an entity by name")
    p_get.add_argument("name", help="Entity name (case-insensitive)")
    p_get.add_argument("--format", choices=["table", "json"], default="table")
    p_get.set_defaults(func=cmd_get)

    # count subcommand — entity totals + by-type rollup (#259)
    p_count = sub.add_parser(
        "count",
        help="Count entities (total + by-type rollup)",
    )
    p_count.add_argument(
        "--type",
        dest="type_filter",
        default=None,
        help="Filter to a single label (prints just the count)",
    )
    p_count.add_argument("--json", action=_STORE_TRUE, help="Emit JSON envelope")
    p_count.set_defaults(func=cmd_count)

    # audit subcommand — one-shot junk/paths/enrichment audit (#260)
    from kairix.use_cases.entity_audit import EntityAuditMode as _AuditMode

    p_audit = sub.add_parser("audit", help="Audit entity graph (junk/paths/enrichment)")
    p_audit.add_argument(
        "--mode",
        choices=[m.value for m in _AuditMode],
        default="all",
        help="Audit lens (default: all = union of junk, paths, enrichment)",
    )
    p_audit.add_argument("--format", choices=["text", "json"], default="text")
    p_audit.add_argument("--output", default=None, help="Write report to FILE instead of stdout")
    p_audit.set_defaults(func=cmd_audit)

    # purge subcommand — DETACH DELETE rows from an audit report (#261)
    p_purge = sub.add_parser("purge", help="Purge entities from a saved audit report")
    p_purge.add_argument(
        "--audit-report",
        required=True,
        help="Path to JSON audit report (from `kairix entity audit --format json --output FILE`)",
    )
    p_purge.add_argument("--format", choices=["text", "json"], default="text")
    purge_action = p_purge.add_mutually_exclusive_group(required=True)
    purge_action.add_argument("--dry-run", action=_STORE_TRUE, help="Preview deletions, run no Cypher")
    purge_action.add_argument("--execute", action=_STORE_TRUE, help="Apply DETACH DELETE for each row")
    p_purge.set_defaults(func=cmd_purge)

    return parser


def cmd_get(
    args: argparse.Namespace,
    *,
    deps: Any = None,
) -> int:
    """kairix entity get <name> — direct Neo4j entity-card lookup.

    Thin adapter around ``kairix.use_cases.entity_get.run_entity_get``.
    """
    import json as _json

    from kairix.use_cases.entity_get import run_entity_get

    out = run_entity_get(args.name, deps=deps)

    if args.format == "json":
        from kairix.use_cases.entity_get import entity_get_output_to_envelope

        print(_json.dumps(entity_get_output_to_envelope(out), indent=2))
    else:
        print(format_get_output(out))

    return 1 if out.error else 0


def format_get_output(out: Any) -> str:
    """Render an EntityGetOutput as the operator-facing table view."""
    if out.error:
        return f"error: {out.error}"
    lines: list[str] = [
        f"Entity:     {out.name}",
        f"Type:       {out.type or '(unknown)'}",
        f"Neo4j id:   {out.id or '(none)'}",
        f"Vault path: {out.vault_path or '(none)'}",
    ]
    if out.summary:
        lines.append("")
        lines.append(out.summary)
    return "\n".join(lines)


def rollup_entity_counts(rows: list[dict[str, Any]]) -> tuple[int, dict[str, int]]:
    """Aggregate raw `MATCH (n) RETURN labels(n), count(n)` rows.

    Each row is ``{"labels": ["Primary", ...], "count": N}``. The primary
    label is the first element of ``labels``; rows with an empty/missing
    labels list roll into the ``"Unlabelled"`` bucket. Returns ``(total,
    by_type)`` where ``by_type`` is sorted alphabetically for stable output.
    """
    by_type: dict[str, int] = {}
    total = 0
    for row in rows:
        labels = row.get("labels") or []
        primary = labels[0] if labels else "Unlabelled"
        count = int(row.get("count", 0))
        by_type[primary] = by_type.get(primary, 0) + count
        total += count
    return total, dict(sorted(by_type.items()))


def format_count_text(total: int, by_type: dict[str, int]) -> str:
    """Render the operator-facing two-line text output for ``entity count``."""
    lines = [f"total_entities: {total}", "by_type:"]
    for label, n in by_type.items():
        lines.append(f"  {label}: {n}")
    return "\n".join(lines)


def cmd_count(
    args: argparse.Namespace,
    *,
    neo4j_client: Any = None,
) -> int:
    """kairix entity count — total + by-type entity counts (#259).

    ``neo4j_client`` is a DI seam for tests; production callers leave it
    ``None`` and the CLI resolves a real client via ``get_client``.
    """
    import json as _json

    if neo4j_client is None:
        from kairix.knowledge.graph.client import get_client

        neo4j: Any = get_client()
    else:
        neo4j = neo4j_client

    if not neo4j.available:
        print("ERROR: Neo4j not available. Check connection settings.", file=sys.stderr)
        return 1

    rows = neo4j.cypher("MATCH (n) RETURN labels(n) AS labels, count(n) AS count")
    total, by_type = rollup_entity_counts(rows)

    if args.type_filter is not None:
        print(by_type.get(args.type_filter, 0))
        return 0

    if args.json:
        print(_json.dumps({"total": total, "by_type": by_type}, indent=2))
        return 0

    print(format_count_text(total, by_type))
    return 0


def cmd_audit(
    args: argparse.Namespace,
    *,
    deps: Any = None,
    neo4j_client: Any = None,
) -> int:
    """kairix entity audit — one-shot junk/paths/enrichment audit (#260).

    Thin adapter around ``kairix.use_cases.entity_audit.run_entity_audit``.
    ``deps`` is the test DI seam; production callers leave it ``None`` and
    the CLI builds an ``EntityAuditDeps`` from the live Neo4j client + the
    configured document root.
    """
    from kairix.use_cases.entity_audit import (
        EntityAuditDeps,
        EntityAuditMode,
        format_report_json,
        format_report_text,
        run_entity_audit,
    )

    if deps is None:
        client = neo4j_client if neo4j_client is not None else _resolve_neo4j_client()
        document_root = _resolve_document_root()
        deps = EntityAuditDeps(neo4j_client=client, document_root=document_root)

    mode = EntityAuditMode(args.mode)
    report = run_entity_audit(mode, deps=deps)

    rendered = format_report_json(report) if args.format == "json" else format_report_text(report)
    if args.output:
        try:
            from pathlib import Path

            Path(args.output).write_text(rendered, encoding="utf-8")
        except OSError as exc:
            print(f"ERROR: could not write output file: {exc}", file=sys.stderr)
            return 1
        print(f"Wrote {report.total} row(s) to {args.output}")
    else:
        print(rendered, end="" if rendered.endswith("\n") else "\n")
    return 0


def cmd_purge(
    args: argparse.Namespace,
    *,
    deps: Any = None,
    neo4j_client: Any = None,
) -> int:
    """kairix entity purge — DETACH DELETE rows from an audit report (#261).

    Reads the JSON audit report ``run_entity_audit`` emits and either
    previews (``--dry-run``) or executes (``--execute``) the deletes.
    """
    from kairix.use_cases.entity_purge import (
        EntityPurgeDeps,
        format_purge_json,
        format_purge_text,
        run_entity_purge,
    )

    if deps is None:
        client = neo4j_client if neo4j_client is not None else _resolve_neo4j_client()
        deps = EntityPurgeDeps(neo4j_client=client)

    result = run_entity_purge(args.audit_report, dry_run=args.dry_run, deps=deps)
    rendered = format_purge_json(result) if args.format == "json" else format_purge_text(result)
    print(rendered, end="" if rendered.endswith("\n") else "\n")
    if result.error:
        return 1
    return 0


def _resolve_neo4j_client() -> Any:
    """Return the production Neo4j client. Isolated so tests can leave it untouched."""
    from kairix.knowledge.graph.client import get_client

    return get_client()


def _resolve_document_root() -> str:
    """Return the configured document root path as a string."""
    try:
        from kairix.paths import document_root

        return str(document_root())
    except Exception:
        return ""


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
    if args.command == "get":
        return cmd_get(args)
    if args.command == "count":
        return cmd_count(args, neo4j_client=neo4j_client)
    if args.command == "audit":
        return cmd_audit(args, neo4j_client=neo4j_client)
    if args.command == "purge":
        return cmd_purge(args, neo4j_client=neo4j_client)
    return 1  # pragma: no cover — unreachable; subparsers required=True
