"""
kairix.knowledge.store.cli — CLI entry point for document store operations.

Usage:
    kairix store crawl [--document-root PATH] [--dry-run] [--verbose]
    kairix store health [--document-root PATH] [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def main(argv: list[str] | None = None, *, neo4j_client: Any = None) -> None:
    """Entry point for `kairix store`.

    The ``neo4j_client`` keyword lets BDD/integration tests inject a
    ``FakeNeo4jClient`` instead of letting the CLI call ``get_client()``
    at the module boundary. Production callers leave it ``None``.
    """
    parser = argparse.ArgumentParser(
        prog="kairix store",
        description="Document store operations: crawl entities into Neo4j, health check",
    )
    sub = parser.add_subparsers(dest="subcommand")

    # ── crawl ────────────────────────────────────────────────────────────────
    crawl_p = sub.add_parser("crawl", help="Crawl document store structure → upsert entities into Neo4j")
    crawl_p.add_argument(
        "--document-root",
        default=None,
        help="Document root directory (default: KAIRIX_DOCUMENT_ROOT env var)",
    )
    crawl_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be written without writing",
    )
    crawl_p.add_argument("--verbose", action="store_true", help="Log each entity discovered")

    # ── health ───────────────────────────────────────────────────────────────
    health_p = sub.add_parser("health", help="Document store and entity graph health summary")
    health_p.add_argument("--document-root", default=None, help="Document root directory")
    health_p.add_argument("--json", dest="json_out", action="store_true", help="Output as JSON")

    args = parser.parse_args(argv)

    if args.subcommand == "crawl":
        _cmd_crawl(args, neo4j_client=neo4j_client)
    elif args.subcommand == "health":
        _cmd_health(args, neo4j_client=neo4j_client)
    else:
        parser.print_help()
        sys.exit(1)


def _resolve_document_root(arg: str | None) -> str:
    from kairix.paths import document_root_override

    if arg:
        return arg
    env = document_root_override()
    if env:
        return env
    print("Error: --document-root or KAIRIX_DOCUMENT_ROOT required", file=sys.stderr)
    sys.exit(1)


def _print_count_line(label: str, found: int, upserted: int, dry_run: bool) -> None:
    """Print one ``label N found[, M upserted | (dry run)]`` line for the crawl summary.

    ``label`` already includes its own ``:`` + alignment whitespace so the caller
    controls column alignment exactly.
    """
    suffix = f", {upserted} upserted" if not dry_run else " (dry run — not written)"
    print(f"  {label}{found} found{suffix}")


def _print_crawl_report(report: Any, document_root: str) -> None:
    """Print the human-readable summary block for a crawl report."""
    mode = "[DRY RUN] " if report.dry_run else ""
    print(f"{mode}Document store crawl complete: {document_root}")
    _print_count_line("Organisations: ", report.organisations_found, report.organisations_upserted, report.dry_run)
    _print_count_line("Persons:       ", report.persons_found, report.persons_upserted, report.dry_run)
    _print_count_line("Outcomes:      ", report.outcomes_found, report.outcomes_upserted, report.dry_run)
    _print_count_line("Edges:         ", report.edges_found, report.edges_upserted, report.dry_run)


def _cmd_crawl(args: argparse.Namespace, *, neo4j_client: Any = None) -> None:
    import logging

    level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(level=level, format="%(levelname)s %(message)s")

    document_root = _resolve_document_root(args.document_root)

    from kairix.knowledge.store.crawler import crawl

    if neo4j_client is None:
        from kairix.knowledge.graph.client import get_client

        neo4j_client = get_client()

    if not neo4j_client.available and not args.dry_run:
        print("Warning: Neo4j unavailable — running in dry-run mode", file=sys.stderr)
        args.dry_run = True

    report = crawl(document_root=document_root, neo4j_client=neo4j_client, dry_run=args.dry_run)
    _print_crawl_report(report, document_root)

    if report.errors:
        print(f"\n  Errors ({len(report.errors)}):")
        for err in report.errors:
            print(f"    - {err}", file=sys.stderr)
        sys.exit(1)

    sys.exit(0)


def _cmd_health(args: argparse.Namespace, *, neo4j_client: Any = None) -> None:
    from kairix.knowledge.store.health import run_store_health

    document_root = args.document_root  # optional for health check

    if neo4j_client is None:
        from kairix.knowledge.graph.client import get_client

        neo4j_client = get_client()
    report = run_store_health(neo4j_client=neo4j_client, document_root=document_root)

    if args.json_out:
        import dataclasses

        payload = dataclasses.asdict(report)
        payload["ok"] = report.ok
        payload["total_entities"] = report.total_entities
        print(json.dumps(payload, indent=2))
    else:
        from kairix.knowledge.store.health import format_health_text

        print(format_health_text(report))

    sys.exit(0 if report.ok else 1)
