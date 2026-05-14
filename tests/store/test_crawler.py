"""
Tests for kairix.knowledge.store.crawler — document-store-to-Neo4j entity crawler.

All Neo4j calls are mocked. Filesystem is provided via tmp_path.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kairix.knowledge.store.crawler import (
    CrawlReport,
    as_list,
    crawl,
    parse_frontmatter,
)
from kairix.utils import display_name, slugify

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _make_neo4j(available: bool = True) -> MagicMock:
    client = MagicMock()
    client.available = available
    client.upsert_organisation.return_value = True
    client.upsert_person.return_value = True
    client.upsert_outcome.return_value = True
    client.upsert_edge.return_value = True
    return client


# ---------------------------------------------------------------------------
# Unit helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_to_slug_basic() -> None:
    assert slugify("Acme Australia") == "acme-australia"


@pytest.mark.unit
def test_to_slug_hyphens_preserved() -> None:
    assert slugify("test-person") == "test-person"


@pytest.mark.unit
def test_to_slug_special_chars() -> None:
    assert slugify("Example Ventures!") == "example-ventures"


@pytest.mark.unit
def testdisplay_name() -> None:
    assert display_name("acme-australia") == "Acme Australia"


@pytest.mark.unit
def test_as_list_none() -> None:
    assert as_list(None) == []


@pytest.mark.unit
def test_as_list_scalar() -> None:
    assert as_list("consulting") == ["consulting"]


@pytest.mark.unit
def test_as_list_list() -> None:
    assert as_list(["a", "b"]) == ["a", "b"]


@pytest.mark.unit
def test_parse_frontmatter_valid(tmp_path: Path) -> None:
    md = _write(
        tmp_path / "test.md",
        "---\nname: Acme\ntier: client\n---\n# Body",
    )
    fm = parse_frontmatter(md)
    assert fm["name"] == "Acme"
    assert fm["tier"] == "client"


@pytest.mark.unit
def test_parse_frontmatter_no_frontmatter(tmp_path: Path) -> None:
    md = _write(tmp_path / "test.md", "# Just content\nno frontmatter")
    fm = parse_frontmatter(md)
    assert fm == {}


@pytest.mark.unit
def test_parse_frontmatter_missing_file() -> None:
    fm = parse_frontmatter(Path("/nonexistent/file.md"))
    assert fm == {}


# ---------------------------------------------------------------------------
# CrawlReport
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_crawl_report_ok_when_no_errors() -> None:
    r = CrawlReport(document_root="/store", dry_run=False)
    assert r.ok is True


@pytest.mark.unit
def test_crawl_report_not_ok_with_errors() -> None:
    r = CrawlReport(document_root="/store", dry_run=False, errors=["oops"])
    assert r.ok is False


# ---------------------------------------------------------------------------
# crawl — organisation discovery
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_crawl_finds_org_from_client_dir(tmp_path: Path) -> None:
    org_dir = tmp_path / "02-Areas" / "00-Clients" / "Acme"
    _write(org_dir / "Acme.md", "---\nname: Acme\ntier: client\n---\n# Acme")

    client = _make_neo4j()
    report = crawl(document_root=tmp_path, neo4j_client=client)

    assert report.organisations_found == 1
    assert report.organisations_upserted == 1
    client.upsert_organisation.assert_called_once()
    node = client.upsert_organisation.call_args[0][0]
    assert node.id == "acme"
    assert node.name == "Acme"


@pytest.mark.unit
def test_crawl_dry_run_does_not_call_upsert(tmp_path: Path) -> None:
    org_dir = tmp_path / "02-Areas" / "00-Clients" / "Acme"
    _write(org_dir / "Acme.md", "---\nname: Acme\n---")

    client = _make_neo4j()
    report = crawl(document_root=tmp_path, neo4j_client=client, dry_run=True)

    assert report.organisations_found == 1
    assert report.organisations_upserted == 0
    client.upsert_organisation.assert_not_called()


@pytest.mark.unit
def test_crawl_org_reads_frontmatter_fields(tmp_path: Path) -> None:
    org_dir = tmp_path / "02-Areas" / "00-Clients" / "Acme"
    _write(
        org_dir / "Acme.md",
        "---\nname: Acme Corp\ntier: partner\nindustry: [technology]\ngeography: [ANZ]\n---\n# Acme",
    )
    client = _make_neo4j()
    crawl(document_root=tmp_path, neo4j_client=client)
    node = client.upsert_organisation.call_args[0][0]
    assert node.tier == "partner"
    assert node.industry == ["technology"]
    assert node.geography == ["ANZ"]


@pytest.mark.unit
def test_crawl_org_fallback_display_name_from_dirname(tmp_path: Path) -> None:
    org_dir = tmp_path / "02-Areas" / "00-Clients" / "example-client"
    _write(org_dir / "index.md", "# Index")  # no name in frontmatter

    client = _make_neo4j()
    crawl(document_root=tmp_path, neo4j_client=client)
    node = client.upsert_organisation.call_args[0][0]
    assert node.name == "Example Client"


@pytest.mark.unit
def test_crawl_no_clients_dir_no_orgs(tmp_path: Path) -> None:
    client = _make_neo4j()
    report = crawl(document_root=tmp_path, neo4j_client=client)
    assert report.organisations_found == 0


# ---------------------------------------------------------------------------
# crawl — person discovery
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_crawl_finds_person_from_people_notes(tmp_path: Path) -> None:
    people = tmp_path / "02-Areas" / "Network" / "People-Notes"
    _write(people / "felicity-herron.md", "---\nname: Felicity Herron\nrole: CTO\n---")

    client = _make_neo4j()
    report = crawl(document_root=tmp_path, neo4j_client=client)

    assert report.persons_found == 1
    assert report.persons_upserted == 1
    node = client.upsert_person.call_args[0][0]
    assert node.id == "felicity-herron"
    assert node.name == "Felicity Herron"
    assert node.role == "CTO"


@pytest.mark.unit
def test_crawl_person_resolves_org(tmp_path: Path) -> None:
    # Org must exist first so resolve works
    org_dir = tmp_path / "02-Areas" / "00-Clients" / "Acme"
    _write(org_dir / "Acme.md", "---\nname: Acme\n---")

    people = tmp_path / "02-Areas" / "Network" / "People-Notes"
    _write(people / "test-person-two.md", "---\nname: Test Person\norg: Acme\n---")

    client = _make_neo4j()
    crawl(document_root=tmp_path, neo4j_client=client)

    person_node = client.upsert_person.call_args[0][0]
    assert person_node.org == "acme"

    # WORKS_AT edge should have been created
    edge_calls = [c[0][0] for c in client.upsert_edge.call_args_list]
    works_at = [e for e in edge_calls if e.kind.value == "WORKS_AT"]
    assert len(works_at) == 1
    assert works_at[0].from_id == "test-person-two"
    assert works_at[0].to_id == "acme"


@pytest.mark.unit
def test_crawl_person_no_org_no_edge(tmp_path: Path) -> None:
    people = tmp_path / "02-Areas" / "Network" / "People-Notes"
    _write(people / "unknown-person.md", "---\nname: Unknown\n---")

    client = _make_neo4j()
    crawl(document_root=tmp_path, neo4j_client=client)

    person_node = client.upsert_person.call_args[0][0]
    assert person_node.org == ""
    edge_calls = client.upsert_edge.call_args_list
    works_at = [c for c in edge_calls if c[0][0].kind.value == "WORKS_AT"]
    assert len(works_at) == 0


# ---------------------------------------------------------------------------
# crawl — outcome discovery
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_crawl_finds_outcome(tmp_path: Path) -> None:
    outcomes = tmp_path / "05-Knowledge" / "01-Domain-Outcomes"
    _write(
        outcomes / "ai-governance.md",
        "---\nname: AI Governance\ndomain: technology\n---",
    )

    client = _make_neo4j()
    report = crawl(document_root=tmp_path, neo4j_client=client)

    assert report.outcomes_found == 1
    client.upsert_outcome.assert_called_once()
    node = client.upsert_outcome.call_args[0][0]
    assert node.id == "ai-governance"
    assert node.domain == "technology"


# ---------------------------------------------------------------------------
# crawl — wikilink edges
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_crawl_creates_mentions_edge_for_wikilink(tmp_path: Path) -> None:
    org_dir = tmp_path / "02-Areas" / "00-Clients" / "Acme"
    _write(org_dir / "Acme.md", "---\nname: Acme\n---")

    # A document that wikilinks [[Acme]]
    docs = tmp_path / "01-Projects"
    _write(
        docs / "insurance-analysis.md",
        "We worked with [[Acme]] on their digital strategy.",
    )

    client = _make_neo4j()
    crawl(document_root=tmp_path, neo4j_client=client)

    edge_calls = [c[0][0] for c in client.upsert_edge.call_args_list]
    mentions = [e for e in edge_calls if e.kind.value == "MENTIONS"]
    assert len(mentions) >= 1
    assert any(e.to_id == "acme" for e in mentions)


@pytest.mark.unit
def test_crawl_ignores_wikilinks_to_unknown_entities(tmp_path: Path) -> None:
    docs = tmp_path / "01-Projects"
    _write(docs / "notes.md", "Reference to [[SomeUnknownOrg]] in passing.")

    client = _make_neo4j()
    crawl(document_root=tmp_path, neo4j_client=client)

    edge_calls = [c[0][0] for c in client.upsert_edge.call_args_list]
    mentions = [e for e in edge_calls if e.kind.value == "MENTIONS"]
    assert len(mentions) == 0


# ---------------------------------------------------------------------------
# crawl — error handling
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_crawl_nonexistent_document_root_returns_error() -> None:
    client = _make_neo4j()
    report = crawl(document_root="/nonexistent/path", neo4j_client=client)
    assert not report.ok
    assert len(report.errors) > 0


@pytest.mark.unit
def test_crawl_upsert_failure_recorded_in_errors(tmp_path: Path) -> None:
    org_dir = tmp_path / "02-Areas" / "00-Clients" / "Acme"
    _write(org_dir / "Acme.md", "---\nname: Acme\n---")

    client = _make_neo4j()
    client.upsert_organisation.return_value = False  # simulate failure

    report = crawl(document_root=tmp_path, neo4j_client=client)
    assert len(report.errors) > 0
    assert any("acme" in e for e in report.errors)


@pytest.mark.unit
def test_crawl_neo4j_unavailable_dry_run_still_counts(tmp_path: Path) -> None:
    org_dir = tmp_path / "02-Areas" / "00-Clients" / "Acme"
    _write(org_dir / "Acme.md", "---\nname: Acme\n---")

    client = _make_neo4j(available=False)
    report = crawl(document_root=tmp_path, neo4j_client=client, dry_run=True)

    assert report.organisations_found == 1
    assert report.organisations_upserted == 0
    client.upsert_organisation.assert_not_called()
