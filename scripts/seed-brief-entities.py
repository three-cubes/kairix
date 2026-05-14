#!/usr/bin/env python3
"""
seed-brief-entities.py — Seed entity cross-references from Research-Brief vault notes.

Scans all Research-Brief notes in $KAIRIX_VAULT_ROOT,
finds `related-entities:` YAML frontmatter lists,
resolves entity names via Neo4j find_by_name(),
and writes MENTIONS_IN_BRIEF edges to Neo4j.

This implements S1-B: Resource entity cross-referencing.

Usage:
    python scripts/seed-brief-entities.py [--dry-run] [--vault-root PATH]
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from kairix.knowledge.graph.client import get_client

VAULT_ROOT = Path(os.environ.get("KAIRIX_VAULT_ROOT", "/data/obsidian-vault"))
BRIEF_DIRS = [
    "03-Resources/Research-Briefs",
    "03-Resources",
    "04-Agent-Knowledge/briefs",
]


def find_brief_dirs(vault_root: Path) -> list[Path]:
    """Find directories that likely contain Research-Brief notes."""
    found = []
    for rel_dir in BRIEF_DIRS:
        candidate = vault_root / rel_dir
        if candidate.exists():
            found.append(candidate)
    return found if found else [vault_root]


def extract_frontmatter(text: str) -> dict:
    """Extract YAML frontmatter from a markdown note. Returns {} on failure."""
    if not text.startswith("---"):
        return {}
    try:
        end = text.index("---", 3)
        fm_text = text[3:end].strip()
        return yaml.safe_load(fm_text) or {}
    except Exception:
        return {}


def extract_related_entities(text: str) -> list[str]:
    """Extract related-entities list from frontmatter."""
    fm = extract_frontmatter(text)
    entities = fm.get("related-entities") or fm.get("related_entities") or []
    if isinstance(entities, str):
        entities = [e.strip() for e in entities.split(",") if e.strip()]
    return [str(e).strip() for e in entities if e]


def resolve_entity(name: str, neo4j_client: object) -> str | None:
    """Resolve entity name to Neo4j node id."""
    for variant in [name, name.lower(), name.title(), name.replace("-", " ").title()]:
        rows = neo4j_client.find_by_name(variant)  # type: ignore[attr-defined] — neo4j_client typed `object` to keep script decoupled; .find_by_name() exists on real client
        if rows:
            return str(rows[0].get("id", ""))
    return None


def seed_brief_entities(dry_run: bool = False, vault_root: Path = VAULT_ROOT) -> None:
    neo4j = get_client()
    if not neo4j.available:
        print("ERROR: Neo4j unavailable — check KAIRIX_NEO4J_URI / KAIRIX_NEO4J_PASSWORD", file=sys.stderr)
        sys.exit(1)

    brief_dirs = find_brief_dirs(vault_root)
    print(f"Scanning brief directories: {[str(d) for d in brief_dirs]}")

    all_briefs = []
    for d in brief_dirs:
        all_briefs.extend(d.rglob("*.md"))
    print(f"Found {len(all_briefs)} markdown notes")

    total_edges = 0
    skipped_no_entities = 0
    skipped_unresolved = 0
    edges: list[tuple[str, str, str]] = []  # (brief_path_str, entity_id, entity_name)

    for brief_path in sorted(all_briefs):
        text = brief_path.read_text(encoding="utf-8")
        entity_names = extract_related_entities(text)
        if not entity_names:
            skipped_no_entities += 1
            continue

        brief_rel = str(brief_path.relative_to(vault_root))
        print(f"\n  {brief_rel}")

        for name in entity_names:
            entity_id = resolve_entity(name, neo4j)
            if not entity_id:
                print(f"    [SKIP] Cannot resolve: {name!r}")
                skipped_unresolved += 1
                continue
            print(f"    -> {name!r} (id={entity_id})")
            edges.append((brief_rel, entity_id, name))

    print(f"\nEdges to write: {len(edges)}")

    if not dry_run and edges:
        now = datetime.now(timezone.utc).isoformat()
        for brief_path_str, entity_id, _entity_name in edges:
            try:
                # Store brief cross-reference as a property on the entity node
                neo4j.cypher(  # type: ignore[attr-defined] — neo4j typed via get_client()'s `object` return; .cypher() exists on real client
                    "MATCH (n {id: $id}) "
                    "SET n.mentioned_in_briefs = coalesce(n.mentioned_in_briefs, []) + [$path], "
                    "    n.brief_last_seen = $ts",
                    {"id": entity_id, "path": brief_path_str, "ts": now},
                )
                total_edges += 1
            except Exception as e:
                print(f"    [ERROR] ({entity_id}): {e}", file=sys.stderr)

        print(f"\nWritten: {total_edges} brief-entity references")
    elif dry_run:
        print("\n[DRY RUN] No changes written.")

    print(f"Stats: no_related_entities={skipped_no_entities} | unresolved={skipped_unresolved}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed brief-entity cross-references into Neo4j")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--vault-root",
        default=str(VAULT_ROOT),
        help="Vault root directory (default: $KAIRIX_VAULT_ROOT)",
    )
    args = parser.parse_args()
    seed_brief_entities(dry_run=args.dry_run, vault_root=Path(args.vault_root))
