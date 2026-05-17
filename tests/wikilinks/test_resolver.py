"""
Tests for kairix.knowledge.wikilinks.resolver

Covers:
- load_entities_from_bootstrap(): parses a synthetic index file (tmp_path fixture)
- load_entities_from_neo4j(): loads from a mock Neo4j client
- get_entities(): Neo4j-prefer / fallback logic via monkeypatch

All tests are fully self-contained — no real document store or Neo4j required.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kairix.knowledge.wikilinks.resolver import (
    WikiEntity,
    get_entities,
    load_entities_from_bootstrap,
    load_entities_from_neo4j,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

SYNTHETIC_BOOTSTRAP = """\
# Wikilink Entity Index

## Clients

| Entity | Link | Vault Path |
|---|---|---|
| Acme Corp | `[[Acme-Corp]]` | `02-Areas/Clients/Acme-Corp/` |
| Zenith Ltd | `[[Zenith-Ltd]]` | `02-Areas/Clients/Zenith-Ltd/` |
| Gamma Systems | `[[Gamma-Systems\\|Gamma Systems]]` | `02-Areas/Clients/Gamma-Systems/` |
| Delta Co | `[[Delta-Co]]` | `02-Areas/Clients/Delta-Co/` |

## Key Organisations

| Entity | Link | Vault Path |
|---|---|---|
| Nexus Digital | `[[NexusDigital]]` | `02-Areas/Work/Orgs/NexusDigital/` |
| Softcorp | `[[Softcorp]]` | `02-Areas/Work/Orgs/Softcorp/` |

## Key People

| Entity | Link | Vault Path |
|---|---|---|
| Jordan Blake | `[[JordanBlake]]` | `02-Areas/People/JordanBlake/` |
| Sam Rivera | `[[SamRivera]]` | `02-Areas/People/SamRivera/` |

## Active Projects

| Entity | Link | Vault Path |
|---|---|---|
| Project Atlas | `[[ProjectAtlas]]` | `01-Projects/Atlas/` |
| Project Beacon | `[[ProjectBeacon]]` | `01-Projects/Beacon/` |

## Frameworks

| Entity | Link | Vault Path |
|---|---|---|
| Triad Method | `[[TriadMethod]]` | `05-Knowledge/Frameworks/TriadMethod/` |
| Relay Framework | `[[RelayFramework]]` | `05-Knowledge/Frameworks/RelayFramework/` |
"""

_NEO4J_ROWS = [
    {
        "id": "acme-health",
        "name": "Acme Corp",
        "aliases": ["Acme"],
        "vault_path": "02-Areas/Clients/Acme-Corp/",
    },
    {
        "id": "zenith-energy",
        "name": "Zenith Ltd",
        "aliases": [],
        "vault_path": "02-Areas/Clients/Zenith-Ltd/",
    },
    {
        "id": "nexus-digital",
        "name": "Nexus Digital",
        "aliases": [],
        "vault_path": "02-Areas/Work/Orgs/NexusDigital/",
    },
    {
        "id": "jordan-blake",
        "name": "Jordan Blake",
        "aliases": [],
        "vault_path": "02-Areas/People/JordanBlake/",
    },
    {
        "id": "project-atlas",
        "name": "Project Atlas",
        "aliases": [],
        "vault_path": "01-Projects/Atlas/",
    },
    {
        "id": "triad-method",
        "name": "Triad Method",
        "aliases": [],
        "vault_path": "05-Knowledge/Frameworks/TriadMethod/",
    },
]


@pytest.fixture()
def bootstrap_file(tmp_path: Path) -> str:
    """Write synthetic bootstrap index to a tmp file and return its path."""
    p = tmp_path / "wikilink-entity-index.md"
    p.write_text(SYNTHETIC_BOOTSTRAP, encoding="utf-8")
    return str(p)


def _make_neo4j_client(rows: list[dict] | None = None) -> MagicMock:
    """Create a mock Neo4j client returning the given entity rows."""
    client = MagicMock()
    client.available = True
    client.cypher.return_value = rows if rows is not None else _NEO4J_ROWS
    return client


# ---------------------------------------------------------------------------
# load_entities_from_bootstrap
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_bootstrap_loads_entities(bootstrap_file: str) -> None:
    """load_entities_from_bootstrap() parses at least 10 entities from the synthetic index."""
    entities = load_entities_from_bootstrap(bootstrap_file)
    assert len(entities) >= 10, f"Expected ≥10 entities, got {len(entities)}"


@pytest.mark.unit
def test_bootstrap_entity_has_required_fields(bootstrap_file: str) -> None:
    """Each entity from bootstrap should have name, link, vault_path set."""
    entities = load_entities_from_bootstrap(bootstrap_file)
    for entity in entities:
        assert entity.name, f"Empty name: {entity}"
        assert entity.link, f"Empty link for {entity.name}"
        assert entity.vault_path, f"Empty vault_path for {entity.name}"
        assert entity.link.startswith("[["), f"Link doesn't start with [[: {entity.link}"
        assert entity.link.endswith("]]"), f"Link doesn't end with ]]: {entity.link}"


@pytest.mark.unit
def test_bootstrap_parses_acme_health(bootstrap_file: str) -> None:
    """Acme Corp should be present with correct link and vault_path."""
    entities = load_entities_from_bootstrap(bootstrap_file)
    acme = next((e for e in entities if e.name == "Acme Corp"), None)
    assert acme is not None, "Acme Corp not found in bootstrap entities"
    assert acme.link == "[[Acme-Corp]]"
    assert "Acme-Corp" in acme.vault_path


@pytest.mark.unit
def test_bootstrap_parses_burger_palace_alias(bootstrap_file: str) -> None:
    """Gamma Systems should parse the display-alias link form correctly."""
    entities = load_entities_from_bootstrap(bootstrap_file)
    bp = next((e for e in entities if "Gamma" in e.name), None)
    assert bp is not None, "Gamma Systems not found in bootstrap entities"
    assert bp.link == "[[Gamma-Systems|Gamma Systems]]"


@pytest.mark.unit
def test_bootstrap_entity_types_populated(bootstrap_file: str) -> None:
    """Entity types should reflect section headings (client, organisation, person, etc.)."""
    entities = load_entities_from_bootstrap(bootstrap_file)
    types = {e.entity_type for e in entities}
    assert len(types) >= 3, f"Expected ≥3 distinct entity types, got {types}"


@pytest.mark.unit
def test_bootstrap_handles_missing_file() -> None:
    """load_entities_from_bootstrap() returns [] for a missing file."""
    entities = load_entities_from_bootstrap("/nonexistent/path/index.md")
    assert entities == []


@pytest.mark.unit
def test_bootstrap_no_header_rows(bootstrap_file: str) -> None:
    """Should not include rows with 'Entity' or 'Name' as the entity name."""
    entities = load_entities_from_bootstrap(bootstrap_file)
    names = [e.name for e in entities]
    assert "Entity" not in names
    assert "Name" not in names


# ---------------------------------------------------------------------------
# load_entities_from_neo4j
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_neo4j_load_returns_entities() -> None:
    """load_entities_from_neo4j(client=fake) returns WikiEntity objects from Neo4j rows.

    The loader calls cypher() twice (once per label — Organisation, Person),
    so we use side_effect to give each call distinct rows. F5-clean: pass
    client= directly, no monkeypatch of the private lookup.
    """
    org_rows = _NEO4J_ROWS[:4]  # organisations
    person_rows = _NEO4J_ROWS[4:]  # people/projects
    client = MagicMock()
    client.available = True
    client.cypher.side_effect = [org_rows, person_rows]

    entities = load_entities_from_neo4j(client=client)
    assert len(entities) == len(_NEO4J_ROWS)
    names = [e.name for e in entities]
    assert "Acme Corp" in names
    assert "Jordan Blake" in names


@pytest.mark.unit
def test_neo4j_load_returns_empty_when_unavailable() -> None:
    """load_entities_from_neo4j returns [] when the injected client is unavailable."""
    client = MagicMock()
    client.available = False

    entities = load_entities_from_neo4j(client=client)
    assert entities == []


@pytest.mark.unit
def test_neo4j_load_merges_aliases() -> None:
    """Aliases list from Neo4j row is included in all_triggers()."""
    row = {
        "id": "acme",
        "name": "Acme Corp",
        "aliases": ["Acme", "AH"],
        "vault_path": "02-Areas/Clients/Acme/",
    }
    # side_effect: first call (Organisation) returns the row, second (Person) returns []
    client = MagicMock()
    client.available = True
    client.cypher.side_effect = [[row], []]

    entities = load_entities_from_neo4j(client=client)
    assert len(entities) == 1
    triggers = entities[0].all_triggers()
    assert "Acme" in triggers
    assert "AH" in triggers


@pytest.mark.unit
def test_neo4j_load_returns_empty_on_cypher_error() -> None:
    """load_entities_from_neo4j returns [] on any cypher exception."""
    client = MagicMock()
    client.available = True
    client.cypher.side_effect = RuntimeError("connection error")

    entities = load_entities_from_neo4j(client=client)
    assert entities == []


# ---------------------------------------------------------------------------
# get_entities: fallback logic
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_entities_uses_neo4j_when_sufficient(bootstrap_file: str) -> None:
    """get_entities() uses Neo4j when it returns >= 5 entities with vault_path.

    Drives the public ``neo4j_loader`` / ``bootstrap_loader`` kwarg seams
    on :func:`get_entities` — F1-clean (no resolver-module monkey-patch).
    """
    neo4j_entities = [
        WikiEntity(
            name=r["name"],
            aliases=r["aliases"],
            vault_path=r["vault_path"],
            link=f"[[{r['name']}]]",
            entity_type="organisation",
        )
        for r in _NEO4J_ROWS
    ]
    entities = get_entities(
        neo4j_loader=lambda client=None: neo4j_entities,
        bootstrap_loader=lambda: load_entities_from_bootstrap(bootstrap_file),
    )
    names = [e.name for e in entities]
    assert "Acme Corp" in names
    # Should have exactly the 6 Neo4j entities, not the 12 bootstrap entries
    assert len(entities) == 6


@pytest.mark.unit
def test_get_entities_falls_back_to_bootstrap_when_neo4j_sparse(bootstrap_file: str) -> None:
    """get_entities() falls back to bootstrap when Neo4j returns < 5 entities."""
    sparse_rows = [
        WikiEntity(
            name="Acme Corp",
            aliases=[],
            vault_path="02-Areas/Clients/Acme-Corp/",
            link="[[Acme-Corp]]",
            entity_type="organisation",
        ),
        WikiEntity(
            name="Zenith Ltd",
            aliases=[],
            vault_path="02-Areas/Clients/Zenith-Ltd/",
            link="[[Zenith-Ltd]]",
            entity_type="organisation",
        ),
    ]
    entities = get_entities(
        neo4j_loader=lambda client=None: sparse_rows,
        bootstrap_loader=lambda: load_entities_from_bootstrap(bootstrap_file),
    )
    # Should have fallen back to bootstrap (12 entries)
    assert len(entities) >= 10, f"Expected fallback to bootstrap, got {len(entities)} entities"


@pytest.mark.unit
def test_get_entities_falls_back_to_bootstrap_when_neo4j_unavailable(bootstrap_file: str) -> None:
    """get_entities() falls back to bootstrap when Neo4j is completely unavailable."""
    entities = get_entities(
        neo4j_loader=lambda client=None: [],
        bootstrap_loader=lambda: load_entities_from_bootstrap(bootstrap_file),
    )
    assert len(entities) >= 10
    names = [e.name for e in entities]
    assert "Acme Corp" in names


# ---------------------------------------------------------------------------
# WikiEntity.all_triggers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_wiki_entity_all_triggers_deduplicates() -> None:
    """all_triggers() should not return duplicate terms."""
    entity = WikiEntity(
        name="Acme Corp",
        aliases=["Acme Corp", "Acme", "AH"],
        vault_path="02-Areas/Clients/Acme-Corp/",
        link="[[Acme-Corp]]",
        entity_type="organisation",
    )
    triggers = entity.all_triggers()
    assert len(triggers) == len(set(triggers)), "Duplicate triggers found"
    assert "Acme Corp" in triggers
    assert "Acme" in triggers
    assert "AH" in triggers


@pytest.mark.unit
def test_wiki_entity_all_triggers_skips_empty() -> None:
    """all_triggers() should not include empty strings."""
    entity = WikiEntity(
        name="Acme Corp",
        aliases=["", "Acme"],
        vault_path="02-Areas/Clients/Acme-Corp/",
        link="[[Acme-Corp]]",
        entity_type="organisation",
    )
    triggers = entity.all_triggers()
    assert "" not in triggers
