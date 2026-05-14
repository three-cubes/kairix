"""
Entity → wikilink resolution for kairix.

Loads WikiEntity records from Neo4j (preferred) or the bootstrap
wikilink-entity-index.md (fallback).

Entity sources:
  Primary: Neo4j graph (Organisation and Person nodes with vault_path)
  Fallback: <document-root>/agent-knowledge/shared/wikilink-entity-index.md
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kairix.knowledge.graph.client import Neo4jClient

DEFAULT_BOOTSTRAP_PATH = "<document-root>/agent-knowledge/shared/wikilink-entity-index.md"

# Minimum number of entities with vault_path required before we prefer the primary source
_DB_THRESHOLD = 5


@dataclass
class WikiEntity:
    """An entity that can be linked in document store markdown files."""

    name: str
    aliases: list[str]  # all names/aliases (including name itself) that trigger this link
    vault_path: str  # e.g. "02-Areas/Clients/Acme-Corp/"
    link: str  # e.g. "[[Acme-Corp]]" or "[[Gamma-Systems|Gamma Systems]]"
    entity_type: str  # organisation, person, project, tool, etc.

    def all_triggers(self) -> list[str]:
        """Return all text strings (name + aliases) that should trigger this wikilink."""
        seen: set[str] = set()
        result: list[str] = []
        for term in [self.name, *self.aliases]:
            if term and term not in seen:
                seen.add(term)
                result.append(term)
        return result


def _make_link(name: str) -> str:
    """Build a plain [[name]] wikilink. No alias needed for simple names."""
    return f"[[{name}]]"


# ---------------------------------------------------------------------------
# Bootstrap loader
# ---------------------------------------------------------------------------

# Matches table rows like:
#   | Acme-Corp | `[[Acme-Corp]]` | `02-Areas/Clients/Acme-Corp/` |
#   | Gamma Systems | `[[Gamma-Systems\|Gamma Systems]]` | `02-Areas/Clients/Gamma-Systems/` |
# NOSONAR: each capture is bounded by a distinct literal
# delimiter (`|` or backtick); no nested quantifiers — backtracking is
# linear in line length. Input is the bootstrap entity-table markdown file.
_TABLE_ROW_RE = re.compile(r"^\|\s*(?P<entity>[^|]+?)\s*\|\s*`(?P<link>\[\[[^\]]+\]\])`\s*\|\s*`(?P<path>[^`]+)`\s*\|")


def load_entities_from_bootstrap(
    index_path: str = DEFAULT_BOOTSTRAP_PATH,
) -> list[WikiEntity]:
    """
    Parse the bootstrap wikilink index markdown table.

    Handles tables from all sections (Clients, Organisations, Projects, etc.).
    Skips header rows, section headers, and malformed lines.

    Parses rows like:
      | Acme-Corp | `[[Acme-Corp]]` | `02-Areas/Clients/Acme-Corp/` |
      | Gamma Systems | `[[Gamma-Systems\\|Gamma Systems]]` | `02-Areas/Clients/Gamma-Systems/` |
    """
    try:
        with open(index_path, encoding="utf-8") as fh:
            content = fh.read()
    except OSError:
        return []

    entities: list[WikiEntity] = []
    seen_names: set[str] = set()

    # Determine section context for entity_type
    current_section = "unknown"
    section_map = {
        "clients": "organisation",
        "key organisations": "organisation",
        "active projects": "project",
        "frameworks": "framework",
        "key people": "person",
        "agent platform": "component",
    }

    for line in content.splitlines():
        # Update section from H2 headers
        if line.startswith("## "):
            heading = line[3:].strip().lower()
            for keyword, etype in section_map.items():
                if keyword in heading:
                    current_section = etype
                    break

        m = _TABLE_ROW_RE.match(line)
        if not m:
            continue

        entity_name = m.group("entity").strip()
        raw_link = m.group("link").strip()
        vault_path = m.group("path").strip()

        # Skip header rows
        if entity_name.lower() in ("entity", "name"):
            continue
        # Skip vault path annotations like "(general reference)" - keep just path part
        # Strip trailing parenthetical notes from vault_path
        # NOSONAR: non-greedy `.*?` bounded by `)` and end-anchor;
        # operates on a single short path string (≤ a few hundred chars).
        vault_path = re.sub(r"\s*\(.*?\)\s*$", "", vault_path).strip()
        if not vault_path or not entity_name:
            continue

        # Unescape \| inside wikilinks (markdown table escaping)
        link = raw_link.replace("\\|", "|")

        # Extract aliases from display text in the link [[target|display]]
        aliases = _extract_aliases(entity_name, link)

        if entity_name in seen_names:
            continue
        seen_names.add(entity_name)

        entities.append(
            WikiEntity(
                name=entity_name,
                aliases=aliases,
                vault_path=vault_path,
                link=link,
                entity_type=current_section,
            )
        )

    return entities


def _extract_aliases(entity_name: str, link: str) -> list[str]:
    """
    Extract alternate trigger strings from the wikilink.

    For [[Gamma-Systems|Gamma Systems]], the display text 'Gamma Systems' is an alias.
    Always excludes entity_name itself (it's the primary trigger).
    """
    aliases: list[str] = []
    # Match [[target|display]] or [[target]]
    m = re.match(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", link)
    if not m:
        return aliases
    target = m.group(1)  # e.g. "Gamma-Systems"
    display = m.group(2)  # e.g. "Gamma Systems" or None

    candidates = []
    if target and target != entity_name:
        candidates.append(target)
    if display and display != entity_name:
        candidates.append(display)

    seen = {entity_name}
    for c in candidates:
        if c not in seen:
            seen.add(c)
            aliases.append(c)

    return aliases


# ---------------------------------------------------------------------------
# Neo4j loader
# ---------------------------------------------------------------------------


def default_neo4j_client() -> Neo4jClient:
    """Thin wrapper around graph.get_client() — the production default.

    Promoted from ``_neo4j_get_client`` (F5): tests inject a fake client
    via ``load_entities_from_neo4j(client=fake)`` rather than monkeypatching
    this lookup.
    """
    from kairix.knowledge.graph.client import get_client

    return get_client()


def load_entities_from_neo4j(client: Neo4jClient | None = None) -> list[WikiEntity]:
    """
    Load entities with vault_path from the Neo4j graph.

    Queries Organisation and Person nodes. Returns empty list if Neo4j is
    unavailable or has no entities with vault_path populated.

    ``client`` is the F1/F5-clean test seam: tests pass a fake; production
    callers omit the kwarg and the live client is constructed.
    """
    try:
        if client is None:
            client = default_neo4j_client()
        if not client.available:
            return []

        entities: list[WikiEntity] = []
        for label in ("Organisation", "Person"):
            rows = client.cypher(
                f"MATCH (n:{label}) WHERE n.vault_path IS NOT NULL AND n.vault_path <> '' "
                "RETURN n.id AS id, n.name AS name, n.aliases AS aliases, "
                "n.vault_path AS vault_path"
            )
            for row in rows:
                name: str = str(row["name"])
                vault_path: str = str(row["vault_path"])
                aliases: list[str] = list(row.get("aliases") or [])
                link = _make_link(name)
                entities.append(
                    WikiEntity(
                        name=name,
                        aliases=aliases,
                        vault_path=vault_path,
                        link=link,
                        entity_type=label.lower(),
                    )
                )
        return entities
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Unified loader
# ---------------------------------------------------------------------------


def get_entities(client: Neo4jClient | None = None) -> list[WikiEntity]:
    """
    Load entities from Neo4j (preferred), then bootstrap index.

    Falls back to the bootstrap index if Neo4j is unavailable or returns
    fewer than _DB_THRESHOLD entities with vault_path populated.

    ``client`` is passed through to ``load_entities_from_neo4j`` — production
    callers omit it for the default Neo4j connection; tests inject fakes.
    """
    # Try Neo4j first
    neo4j_entities = load_entities_from_neo4j(client=client)
    if len(neo4j_entities) >= _DB_THRESHOLD:
        return neo4j_entities

    # Fallback to bootstrap
    return load_entities_from_bootstrap()
