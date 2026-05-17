"""
kairix.knowledge.store.cli — CLI entry point for document store operations.

Usage:
    kairix store crawl [--document-root PATH] [--dry-run] [--verbose]
                       [--reset [--confirm]]
    kairix store health [--document-root PATH] [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from typing import Any


def _default_crawl_fn(**kwargs: Any) -> Any:
    """Production crawl adapter — defers the heavy crawler import until call time."""
    from kairix.knowledge.store.crawler import crawl

    return crawl(**kwargs)


def main(
    argv: list[str] | None = None,
    *,
    neo4j_client: Any = None,
    noninteractive: bool | None = None,
    crawl_fn: Callable[..., Any] = _default_crawl_fn,
) -> None:
    """Entry point for `kairix store`.

    The ``neo4j_client`` keyword lets BDD/integration tests inject a
    ``FakeNeo4jClient`` instead of letting the CLI call ``get_client()``
    at the module boundary. Production callers leave it ``None``.

    ``noninteractive`` is the F2-clean seam for the ``--reset`` safety
    interlock. When ``None`` (production), the env var
    ``KAIRIX_NONINTERACTIVE`` is consulted via :func:`kairix.paths.noninteractive_mode`
    so operators can bypass the ``--confirm`` requirement in scripted
    pipelines. Tests pass an explicit bool to exercise both paths without
    monkeypatching the environment.

    ``crawl_fn`` is the public DI seam for the crawl adapter. Production
    callers leave it at :func:`_default_crawl_fn`; tests pass a stub to
    drive ``_cmd_crawl`` without monkey-patching
    ``kairix.knowledge.store.crawler.crawl``.
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
    crawl_p.add_argument(
        "--reset",
        action="store_true",
        help="DETACH DELETE every node + relationship before crawling (destructive). Requires --confirm or KAIRIX_NONINTERACTIVE=1.",
    )
    crawl_p.add_argument(
        "--confirm",
        action="store_true",
        help="Required interlock for --reset — without it (or KAIRIX_NONINTERACTIVE=1) the reset is refused.",
    )

    # ── health ───────────────────────────────────────────────────────────────
    health_p = sub.add_parser("health", help="Document store and entity graph health summary")
    health_p.add_argument("--document-root", default=None, help="Document root directory")
    health_p.add_argument("--json", dest="json_out", action="store_true", help="Output as JSON")

    args = parser.parse_args(argv)

    if args.subcommand == "crawl":
        _cmd_crawl(args, neo4j_client=neo4j_client, noninteractive=noninteractive, crawl_fn=crawl_fn)
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
    if getattr(report, "reset_nodes_deleted", None) is not None:
        nodes = report.reset_nodes_deleted
        rels = report.reset_relationships_deleted
        if report.dry_run:
            print(f"{mode}Reset: would DETACH DELETE every node + relationship (dry run — graph untouched)")
        else:
            print(f"{mode}Reset: deleted {nodes} entities, {rels} relationships before crawl")
    print(f"{mode}Document store crawl complete: {document_root}")
    _print_count_line("Organisations: ", report.organisations_found, report.organisations_upserted, report.dry_run)
    _print_count_line("Persons:       ", report.persons_found, report.persons_upserted, report.dry_run)
    _print_count_line("Outcomes:      ", report.outcomes_found, report.outcomes_upserted, report.dry_run)
    _print_count_line("Edges:         ", report.edges_found, report.edges_upserted, report.dry_run)
    _print_override_coverage(report)


def _print_override_coverage(report: Any) -> None:
    """Print the override-coverage summary lines (#263) when a report carries one."""
    coverage = getattr(report, "override_coverage", None)
    if coverage is None:
        return
    never = coverage.never_matched
    print(
        f"  Override coverage: {coverage.matched}/{coverage.total_overrides} overrides matched "
        f"({len(never)} never used)"
    )
    if never:
        sample = never[:10]
        suffix = "" if len(never) <= 10 else f", +{len(never) - 10} more"
        print(f"  Never-matched: {sample}{suffix}")
    path = getattr(report, "override_coverage_path", None)
    if path:
        print(f"  Coverage report written: {path}")


def _resolve_noninteractive(flag: bool | None) -> bool:
    """Resolve the noninteractive flag: explicit caller wins, env var falls back."""
    if flag is not None:
        return flag
    from kairix.paths import noninteractive_mode

    return noninteractive_mode()


def _guard_reset_interlock(args: argparse.Namespace, *, noninteractive: bool) -> None:
    """Block --reset unless --confirm is set or noninteractive mode is in effect.

    Exits 2 with an actionable message on refusal — never silent. The
    exit code is distinct from the standard "errors during crawl" exit 1
    so wrappers can distinguish "we refused to run" from "we ran and
    something inside failed."
    """
    if not getattr(args, "reset", False):
        return
    if args.confirm or noninteractive:
        return
    print(
        "Error: --reset is destructive. Pass --confirm to acknowledge or set "
        "KAIRIX_NONINTERACTIVE=1 in non-interactive pipelines.",
        file=sys.stderr,
    )
    print(
        "  run: kairix store crawl --reset --confirm  (or: KAIRIX_NONINTERACTIVE=1 kairix store crawl --reset)",
        file=sys.stderr,
    )
    sys.exit(2)


def _resolve_overrides(document_root: str) -> Any:
    """Resolve and load the entity-overrides file rooted at the crawl document_root.

    Returns ``EntityOverrides`` (possibly empty when the file is absent).
    Path resolution lives in :func:`kairix.paths.entity_overrides_path`
    (F4: env reads stay in ``paths.py``) — the CLI passes its resolved
    ``document_root`` so the overrides file is anchored to the same root
    the crawl is about to walk.
    """
    from kairix.knowledge.entities.overrides import load_entity_overrides
    from kairix.paths import entity_overrides_path

    path = entity_overrides_path(document_root_arg=document_root)
    return load_entity_overrides(path)


def _cmd_crawl(
    args: argparse.Namespace,
    *,
    neo4j_client: Any = None,
    noninteractive: bool | None = None,
    crawl_fn: Callable[..., Any] = _default_crawl_fn,
) -> None:
    import logging

    level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(level=level, format="%(levelname)s %(message)s")

    resolved_noninteractive = _resolve_noninteractive(noninteractive)
    _guard_reset_interlock(args, noninteractive=resolved_noninteractive)

    document_root = _resolve_document_root(args.document_root)

    if neo4j_client is None:
        from kairix.knowledge.graph.client import get_client

        neo4j_client = get_client()

    if not neo4j_client.available and not args.dry_run:
        print("Warning: Neo4j unavailable — running in dry-run mode", file=sys.stderr)
        args.dry_run = True

    overrides = _resolve_overrides(document_root)

    report = crawl_fn(
        document_root=document_root,
        neo4j_client=neo4j_client,
        dry_run=args.dry_run,
        reset=args.reset,
        overrides=overrides,
    )
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
