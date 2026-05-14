"""
kairix.knowledge.reflib.cli — CLI entry point for reference library operations.

Usage:
    kairix reference-library install [--reflib-root PATH] [--dry-run] [--verbose]
    kairix reference-library status  [--reflib-root PATH] [--json]
    kairix reference-library extract [--reflib-root PATH] [--collection NAME]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from kairix.paths import reflib_root_override

_REFLIB_ROOT_HELP = "Reference library root directory (default: KAIRIX_REFLIB_ROOT env var)"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="kairix reference-library",
        description="Reference library: install entities into Neo4j, check status, run extraction",
    )
    sub = parser.add_subparsers(dest="subcommand")

    # ── install ─────────────────────────────────────────────────────────────
    install_p = sub.add_parser(
        "install",
        help="Load extracted entities from the reference library into Neo4j",
    )
    install_p.add_argument(
        "--reflib-root",
        default=None,
        help=_REFLIB_ROOT_HELP,
    )
    install_p.add_argument("--dry-run", action="store_true", help="Validate without writing to Neo4j")
    install_p.add_argument("--verbose", action="store_true", help="Show per-entity detail")

    # ── status ──────────────────────────────────────────────────────────────
    status_p = sub.add_parser(
        "status",
        help="Show reference library installation status and entity counts",
    )
    status_p.add_argument(
        "--reflib-root",
        default=None,
        help=_REFLIB_ROOT_HELP,
    )
    status_p.add_argument("--json", dest="json_out", action="store_true", help="Output as JSON")

    # ── extract (placeholder) ───────────────────────────────────────────────
    extract_p = sub.add_parser(
        "extract",
        help="Run entity extraction on reference library collections (placeholder)",
    )
    extract_p.add_argument(
        "--reflib-root",
        default=None,
        help=_REFLIB_ROOT_HELP,
    )
    extract_p.add_argument(
        "--collection",
        default=None,
        help="Extract entities from a specific collection only",
    )

    args = parser.parse_args(argv)

    if args.subcommand == "install":
        _cmd_install(args)
    elif args.subcommand == "status":
        _cmd_status(args)
    elif args.subcommand == "extract":
        _cmd_extract(args)
    else:
        parser.print_help()
        sys.exit(1)


def _resolve_reflib_root(arg: str | None) -> str:
    """Resolve the reference library root from arg or env var."""
    if arg:
        return arg
    env = reflib_root_override()
    if env:
        return env
    print("Error: --reflib-root or KAIRIX_REFLIB_ROOT required", file=sys.stderr)
    sys.exit(1)


def _cmd_install(args: argparse.Namespace) -> None:
    import logging

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")
    else:
        logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    reflib_root = Path(_resolve_reflib_root(args.reflib_root))
    entities_dir = reflib_root / "entities"
    nodes_path = entities_dir / "nodes.json"
    edges_path = entities_dir / "edges.json"

    if not entities_dir.is_dir():
        print(f"Error: entities directory not found at {entities_dir}", file=sys.stderr)
        print(
            "Run 'kairix reference-library extract' first to generate entity files.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not nodes_path.exists() and not edges_path.exists():
        print(f"Error: no entity files found in {entities_dir}", file=sys.stderr)
        print("Expected nodes.json and/or edges.json.", file=sys.stderr)
        sys.exit(1)

    from kairix.knowledge.graph.client import get_client
    from kairix.knowledge.reflib.loader import load_entity_stubs

    neo4j_client = get_client()

    if not neo4j_client.available and not args.dry_run:
        print("Warning: Neo4j unavailable — running in dry-run mode", file=sys.stderr)
        args.dry_run = True

    report = load_entity_stubs(
        nodes_path=nodes_path,
        edges_path=edges_path,
        neo4j_client=neo4j_client,
        dry_run=args.dry_run,
    )

    mode = "[DRY RUN] " if args.dry_run else ""
    print(f"{mode}Reference library entity install complete")
    print(f"  Nodes loaded:  {report.nodes_loaded}")
    print(f"  Nodes skipped: {report.nodes_skipped}")
    print(f"  Edges loaded:  {report.edges_loaded}")
    print(f"  Edges skipped: {report.edges_skipped}")

    if report.errors:
        print(f"\n  Errors ({len(report.errors)}):")
        for err in report.errors:
            print(f"    - {err}", file=sys.stderr)
        sys.exit(1)

    sys.exit(0)


def _cmd_status(args: argparse.Namespace) -> None:
    reflib_root = Path(_resolve_reflib_root(args.reflib_root))
    entities_dir = reflib_root / "entities"

    status: dict[str, Any] = {
        "reflib_root": str(reflib_root),
        "entities_dir_exists": entities_dir.is_dir(),
        "collections": _discover_collections(reflib_root),
        "nodes_file": None,
        "edges_file": None,
        "node_count": 0,
        "edge_count": 0,
        "last_modified": None,
    }

    _read_entity_files(entities_dir, status)

    if args.json_out:
        print(json.dumps(status, indent=2))
    else:
        print(_format_status_text(status))

    sys.exit(0)


def _discover_collections(reflib_root: Path) -> list[str]:
    """Walk top-level dirs in reflib root and return collection names."""
    if not reflib_root.is_dir():
        return []
    return sorted(d.name for d in reflib_root.iterdir() if d.is_dir() and not d.name.startswith((".", "_")))


def _read_entity_files(entities_dir: Path, status: dict[str, Any]) -> None:
    """Read nodes.json and edges.json, updating status dict in place."""
    nodes_path = entities_dir / "nodes.json"
    edges_path = entities_dir / "edges.json"

    if nodes_path.exists():
        try:
            nodes_data = json.loads(nodes_path.read_text(encoding="utf-8"))
            status["nodes_file"] = str(nodes_path)
            status["node_count"] = len(nodes_data)
            mtime = datetime.fromtimestamp(nodes_path.stat().st_mtime)
            status["last_modified"] = mtime.isoformat()
        except (json.JSONDecodeError, OSError):
            status["nodes_file"] = f"{nodes_path} (unreadable)"

    if edges_path.exists():
        try:
            edges_data = json.loads(edges_path.read_text(encoding="utf-8"))
            status["edges_file"] = str(edges_path)
            status["edge_count"] = len(edges_data)
        except (json.JSONDecodeError, OSError):
            status["edges_file"] = f"{edges_path} (unreadable)"


def _format_status_text(status: dict[str, Any]) -> str:
    """Format status dict as human-readable text."""
    lines = [
        "Reference Library Status",
        f"  Root:       {status['reflib_root']}",
        f"  Collections: {len(status['collections'])}",
    ]
    for c in status["collections"]:
        lines.append(f"    - {c}")
    lines.append(f"  Entities dir: {'yes' if status['entities_dir_exists'] else 'no'}")
    lines.append(f"  Nodes:  {status['node_count']}")
    lines.append(f"  Edges:  {status['edge_count']}")
    if status["last_modified"]:
        lines.append(f"  Last indexed: {status['last_modified']}")
    return "\n".join(lines)


def _cmd_extract(args: argparse.Namespace) -> None:
    reflib_root = _resolve_reflib_root(args.reflib_root)
    collection = args.collection

    print("Entity extraction is not yet implemented.")
    if collection:
        print(f"  Would extract from collection: {collection}")
    else:
        print(f"  Would extract from all collections in: {reflib_root}")
    print("This command will be wired once the extraction pipeline is ready.")
    sys.exit(0)
