#!/usr/bin/env python3
"""
prune-entities.py — Remove stale/noise entity nodes from Neo4j.

DEPRECATED (2026-05-14): superseded by ``kairix entity audit`` (#260) +
``kairix entity purge`` (#261). The new commands share the same audit
shape and are protocol-driven / unit-tested at the F7 90% floor.

Migration:
    kairix entity audit --mode all --format json --output /tmp/audit.json
    kairix entity purge --audit-report /tmp/audit.json --dry-run
    kairix entity purge --audit-report /tmp/audit.json --execute

This script remains as a thin compatibility shim for operators with
existing automation. It will be removed in a future release.

An entity node is stale if its vault_path property points to a file that no
longer exists on disk, or if it has no vault_path and no summary (never enriched).

Implements the stubs-as-source-of-truth pattern: the graph should reflect what
is curated in vault stubs. Nodes whose stub was deleted are candidates for removal.

Usage:
    python scripts/prune-entities.py [--vault-root PATH] [--execute]

Defaults:
    --vault-root  $KAIRIX_VAULT_ROOT or /data/obsidian-vault
    --execute     dry-run by default (omit flag to preview only)
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class EntityNode:
    id: str
    name: str
    label: str
    vault_path: str | None
    summary: str | None


class PruneReason:
    FILE_MISSING = "file_missing"
    NO_STUB_NO_SUMMARY = "no_stub_no_summary"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prune stale/noise entity nodes from Neo4j.",
    )
    parser.add_argument(
        "--vault-root",
        default=os.environ.get("KAIRIX_VAULT_ROOT", "/data/obsidian-vault"),
        help="Root of the Obsidian vault (default: $KAIRIX_VAULT_ROOT)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        default=False,
        help="Apply deletions (default: dry-run, print report only)",
    )
    return parser.parse_args()


def load_entity_nodes(neo4j_client: object) -> list[EntityNode]:
    """Load all entity nodes from Neo4j."""
    labels = ("Organisation", "Person", "Outcome", "Concept", "Project")
    label_filter = "['" + "','".join(labels) + "']"
    rows = neo4j_client.cypher(  # type: ignore[attr-defined] — neo4j_client typed `object` to keep script decoupled; .cypher() exists on real client
        f"MATCH (n) WHERE labels(n)[0] IN {label_filter} "
        "RETURN n.id AS id, n.name AS name, labels(n)[0] AS label, "
        "n.vault_path AS vault_path, n.summary AS summary"
    )
    return [
        EntityNode(
            id=str(r.get("id") or ""),
            name=str(r.get("name") or ""),
            label=str(r.get("label") or "unknown"),
            vault_path=r.get("vault_path") or None,
            summary=r.get("summary") or None,
        )
        for r in rows
    ]


def classify_entities(
    entities: list[EntityNode],
    vault_root: str,
) -> tuple[list[tuple[EntityNode, str]], list[EntityNode]]:
    """Classify nodes into delete candidates and keepers."""
    to_delete: list[tuple[EntityNode, str]] = []
    to_keep: list[EntityNode] = []
    vault = Path(vault_root)

    for entity in entities:
        if entity.vault_path:
            stub_path = vault / entity.vault_path
            if not stub_path.exists():
                to_delete.append((entity, PruneReason.FILE_MISSING))
                continue
        elif not entity.summary:
            to_delete.append((entity, PruneReason.NO_STUB_NO_SUMMARY))
            continue
        to_keep.append(entity)

    return to_delete, to_keep


def print_dry_run_report(
    to_delete: list[tuple[EntityNode, str]],
    to_keep: list[EntityNode],
) -> None:
    total = len(to_delete) + len(to_keep)
    print(f"\nEntity nodes scanned: {total}")
    print(f"  To delete:          {len(to_delete)}")
    print(f"  To keep:            {len(to_keep)}")

    if to_delete:
        print("\n--- NODES TO DELETE ---")
        col_name = max((len(e.name) for e, _ in to_delete), default=4)
        col_type = max((len(e.label) for e, _ in to_delete), default=4)
        print(f"  {'NAME':<{col_name}}  {'TYPE':<{col_type}}  REASON")
        print("  " + "-" * (col_name + col_type + 10))
        for entity, reason in sorted(to_delete, key=lambda x: (x[1], x[0].label, x[0].name)):
            print(f"  {entity.name:<{col_name}}  {entity.label:<{col_type}}  {reason}")

    if to_keep:
        print("\n--- NODES TO KEEP ---")
        col_name = max((len(e.name) for e in to_keep), default=4)
        col_type = max((len(e.label) for e in to_keep), default=4)
        print(f"  {'NAME':<{col_name}}  {'TYPE':<{col_type}}")
        print("  " + "-" * (col_name + col_type + 4))
        for entity in sorted(to_keep, key=lambda e: (e.label, e.name)):
            print(f"  {entity.name:<{col_name}}  {entity.label:<{col_type}}")

    print("\nRun with --execute to apply deletions.")


def execute_pruning(
    neo4j_client: object,
    to_delete: list[tuple[EntityNode, str]],
    total_before: int,
) -> None:
    print(f"\nDeleting {len(to_delete)} entity nodes...")
    deleted = 0
    for entity, reason in to_delete:
        if not entity.id:
            print(f"  SKIP (no id): {entity.name!r} — {reason}")
            continue
        try:
            neo4j_client.cypher(  # type: ignore[attr-defined] — neo4j_client typed `object` to keep script decoupled; .cypher() exists on real client
                "MATCH (n {id: $id}) DETACH DELETE n",
                {"id": entity.id},
            )
            print(f"  Deleted: {entity.name!r} ({entity.label}) — {reason}")
            deleted += 1
        except Exception as exc:
            print(f"  ERROR deleting {entity.name!r}: {exc}", file=sys.stderr)

    print(f"\nDone. Deleted {deleted} / {total_before} nodes.")
    print("Run `kairix curator health` to verify graph state.")


def main() -> None:
    args = parse_args()

    sys.path.insert(0, str(Path(__file__).parent.parent))
    try:
        from kairix.knowledge.graph.client import get_client
    except ImportError as exc:
        print(f"ERROR: could not import kairix.knowledge.graph.client — {exc}", file=sys.stderr)
        print("Ensure kairix[neo4j] is installed.", file=sys.stderr)
        sys.exit(1)

    neo4j = get_client()
    if not neo4j.available:
        print("ERROR: Neo4j is unavailable. Check KAIRIX_NEO4J_URI / KAIRIX_NEO4J_PASSWORD.", file=sys.stderr)
        sys.exit(1)

    print(f"Vault root: {args.vault_root}")
    print(f"Mode:       {'EXECUTE' if args.execute else 'DRY-RUN'}")

    entities = load_entity_nodes(neo4j)
    total_before = len(entities)

    if total_before == 0:
        print("\nNo entity nodes found in Neo4j. Nothing to do.")
        return

    to_delete, to_keep = classify_entities(entities, args.vault_root)

    if args.execute:
        if not to_delete:
            print(f"\nNo stale nodes found. All {total_before} nodes are healthy.")
        else:
            execute_pruning(neo4j, to_delete, total_before)
    else:
        print_dry_run_report(to_delete, to_keep)


if __name__ == "__main__":
    main()
