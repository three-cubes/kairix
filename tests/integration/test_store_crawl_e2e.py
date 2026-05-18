"""End-to-end integration tests for ``kairix.knowledge.store`` crawl.

Wires the production ``crawl`` function against a real on-disk fixture
tree (a tmp_path-rooted Obsidian-shaped vault) and a writable
``_WritingFakeNeo4jClient`` at the system boundary. Real path discovery,
real frontmatter parsing, real wikilink edge extraction — only the
Neo4j writes are intercepted.

What's covered here that unit + BDD don't catch:
  - A multi-directory crawl (orgs + persons + outcomes + wikilinks)
    runs as a single composition and lands the expected entity set.
  - Re-running the crawl against the same tree is idempotent — same
    upsert call sequence each pass, no new node ids appear.
  - The ``CrawlReport`` rolls up the per-directory counts coherently.
  - Wikilinks resolve through to ``MENTIONS`` edges only when their
    target is a known entity (no phantom edges).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kairix.knowledge.store.crawler import crawl

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Writable boundary fake — records every crawl-side upsert call
# ---------------------------------------------------------------------------


class _WritingFakeNeo4jClient:
    """Boundary fake that satisfies the subset of the Neo4jClient interface
    the production ``crawl`` driver invokes.

    The canonical ``FakeNeo4jClient`` exposes ``upsert_organisation(**kwargs)``
    which doesn't accept positional ``OrganisationNode`` args. The crawler
    passes nodes positionally, so this fake provides the matching surface
    and records every call for assertion.
    """

    available: bool = True

    def __init__(self) -> None:
        self.org_calls: list[Any] = []
        self.person_calls: list[Any] = []
        self.outcome_calls: list[Any] = []
        self.edge_calls: list[Any] = []

    def upsert_organisation(self, node: Any) -> bool:
        self.org_calls.append(node)
        return True

    def upsert_person(self, node: Any) -> bool:
        self.person_calls.append(node)
        return True

    def upsert_outcome(self, node: Any) -> bool:
        self.outcome_calls.append(node)
        return True

    def upsert_edge(self, edge: Any) -> bool:
        self.edge_calls.append(edge)
        return True


# ---------------------------------------------------------------------------
# Fixture vault builder
# ---------------------------------------------------------------------------


def _build_fixture_vault(root: Path) -> None:
    """Create a small Obsidian-shaped vault tree at ``root``.

    Contains:
      - One org under ``02-Areas/00-Clients/Acme/``
      - One org under ``02-Areas/00-Clients/Globex/``
      - One person under ``02-Areas/Network/People-Notes/``
      - One outcome under ``05-Knowledge/01-Domain-Outcomes/``
      - One project doc that wikilinks ``[[Acme]]`` and a phantom
    """
    acme = root / "02-Areas" / "00-Clients" / "Acme"
    acme.mkdir(parents=True)
    (acme / "Acme.md").write_text(
        "---\nname: Acme\ntier: client\nindustry: [technology]\n---\n# Acme",
        encoding="utf-8",
    )

    globex = root / "02-Areas" / "00-Clients" / "Globex"
    globex.mkdir(parents=True)
    (globex / "Globex.md").write_text(
        "---\nname: Globex\ntier: partner\n---\n# Globex",
        encoding="utf-8",
    )

    people = root / "02-Areas" / "Network" / "People-Notes"
    people.mkdir(parents=True)
    (people / "alice-acme.md").write_text(
        "---\nname: Alice Acme\nrole: CTO\norg: Acme\n---\n# Alice",
        encoding="utf-8",
    )

    outcomes = root / "05-Knowledge" / "01-Domain-Outcomes"
    outcomes.mkdir(parents=True)
    (outcomes / "ai-governance.md").write_text(
        "---\nname: AI Governance\ndomain: technology\n---\n# Governance",
        encoding="utf-8",
    )

    proj = root / "01-Projects"
    proj.mkdir(parents=True)
    (proj / "engagement.md").write_text(
        "We worked with [[Acme]] and also referenced [[NotARealOrg]] in passing.\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Crawl integration
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_crawl_against_fixture_vault_populates_expected_entity_set(tmp_path: Path) -> None:
    """A full crawl over the fixture vault discovers each entity and
    lands it in the writable Neo4j fake.

    Sabotage: if ``crawl_organisations`` stopped reading
    ``02-Areas/00-Clients`` (e.g. typo in the directory constant), no
    org upserts would fire and the assertion would catch it.
    """
    _build_fixture_vault(tmp_path)
    client = _WritingFakeNeo4jClient()

    report = crawl(document_root=tmp_path, neo4j_client=client)

    assert report.ok is True
    assert report.organisations_found == 2
    assert report.organisations_upserted == 2
    assert report.persons_found == 1
    assert report.persons_upserted == 1
    assert report.outcomes_found == 1

    # Names landed in the fake, not just counts.
    org_names = sorted(node.name for node in client.org_calls)
    assert org_names == ["Acme", "Globex"]
    person_names = [node.name for node in client.person_calls]
    assert person_names == ["Alice Acme"]
    outcome_names = [node.name for node in client.outcome_calls]
    assert outcome_names == ["AI Governance"]


@pytest.mark.integration
def test_crawl_is_idempotent_on_repeated_runs(tmp_path: Path) -> None:
    """Running ``crawl`` twice against the same tree produces the same
    upsert payload set both times (production Neo4j MERGE collapses
    duplicates server-side; the fake records each call but identity is
    stable).

    Sabotage: if the crawler stopped slugging the org id and instead
    used a timestamp, the second pass would emit different ids.
    """
    _build_fixture_vault(tmp_path)

    client_a = _WritingFakeNeo4jClient()
    crawl(document_root=tmp_path, neo4j_client=client_a)

    client_b = _WritingFakeNeo4jClient()
    crawl(document_root=tmp_path, neo4j_client=client_b)

    # Identity is stable across runs.
    ids_a = sorted(node.id for node in client_a.org_calls)
    ids_b = sorted(node.id for node in client_b.org_calls)
    assert ids_a == ids_b == ["acme", "globex"]

    person_ids_a = sorted(node.id for node in client_a.person_calls)
    person_ids_b = sorted(node.id for node in client_b.person_calls)
    assert person_ids_a == person_ids_b == ["alice-acme"]


@pytest.mark.integration
def test_wikilink_emits_mentions_edge_only_for_known_entity(tmp_path: Path) -> None:
    """``[[Acme]]`` lands a ``MENTIONS`` edge because Acme is a known org;
    ``[[NotARealOrg]]`` is dropped because the slug doesn't resolve.

    Sabotage: if ``_resolve_link_target`` stopped checking the orgs
    dict and accepted every wikilink target, a phantom NotARealOrg edge
    would appear in ``client.edge_calls``.
    """
    _build_fixture_vault(tmp_path)
    client = _WritingFakeNeo4jClient()

    crawl(document_root=tmp_path, neo4j_client=client)

    mentions_edges = [e for e in client.edge_calls if e.kind.value == "MENTIONS"]
    assert len(mentions_edges) >= 1
    targets = {e.to_id for e in mentions_edges}
    assert "acme" in targets
    assert "notarealorg" not in targets

    # WORKS_AT edge from Alice → Acme is also present (org resolution flow).
    works_at = [e for e in client.edge_calls if e.kind.value == "WORKS_AT"]
    assert len(works_at) == 1
    assert works_at[0].from_id == "alice-acme"
    assert works_at[0].to_id == "acme"


@pytest.mark.integration
def test_crawl_dry_run_discovers_but_writes_nothing(tmp_path: Path) -> None:
    """``dry_run=True`` walks the tree and rolls counts in the report
    but the writable boundary fake records zero upsert calls.

    Sabotage: if a refactor accidentally bypassed the ``dry_run`` guard
    in ``crawl_organisations``, the org_calls list would be populated.
    """
    _build_fixture_vault(tmp_path)
    client = _WritingFakeNeo4jClient()

    report = crawl(document_root=tmp_path, neo4j_client=client, dry_run=True)

    assert report.dry_run is True
    assert report.organisations_found == 2
    assert report.persons_found == 1
    assert report.outcomes_found == 1
    # Zero writes despite full discovery — the dry-run contract.
    assert client.org_calls == []
    assert client.person_calls == []
    assert client.outcome_calls == []
    assert client.edge_calls == []
