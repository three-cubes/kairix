"""Tests for kairix.knowledge.reflib.extract — rule-based entity extraction."""

from __future__ import annotations

from pathlib import Path

import pytest

from kairix.knowledge.reflib.extract import (
    RawEntity,
    RawRelationship,
    domain_from_path,
    extract_from_frontmatter,
    extract_from_headings,
    extract_seed_entities,
    is_stop_heading,
    scan_reference_library,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_md(title: str, source: str, domain: str, subdomain: str, body: str) -> str:
    return (
        f'---\ntitle: "{title}"\nsource: {source}\n'
        f"source_url: https://example.com\nlicence: MIT\n"
        f"domain: {domain}\nsubdomain: {subdomain}\n"
        f"date_added: 2026-04-25\n---\n\n{body}\n"
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDomainFromPath:
    @pytest.mark.unit
    def test_engineering_collection(self):
        assert domain_from_path("engineering/foo/bar.md") == "software-engineering"

    @pytest.mark.unit
    def test_philosophy_collection(self):
        assert domain_from_path("philosophy/classical/text.md") == "philosophy"

    @pytest.mark.unit
    def test_unknown_collection(self):
        assert domain_from_path("unknown-col/x.md") == "unknown-col"


class TestStopHeading:
    @pytest.mark.unit
    def test_generic_headings_are_stopped(self):
        assert is_stop_heading("Overview") is True
        assert is_stop_heading("Introduction") is True
        assert is_stop_heading("Getting Started") is True

    @pytest.mark.unit
    def test_short_headings_stopped(self):
        assert is_stop_heading("AB") is True

    @pytest.mark.unit
    def test_valid_heading_passes(self):
        assert is_stop_heading("Twelve-Factor Application Design") is False

    @pytest.mark.unit
    def test_lowercase_heading_stopped(self):
        assert is_stop_heading("some lowercase heading") is True


class TestFrontmatterExtraction:
    @pytest.mark.unit
    def test_extracts_document_and_org(self):
        fm = {
            "title": "OpenTelemetry Basics",
            "source": "OpenTelemetry Documentation",
            "domain": "engineering",
        }
        entities: list[RawEntity] = []
        rels: list[RawRelationship] = []
        extract_from_frontmatter(fm, "engineering/otel/basics.md", "software-engineering", entities, rels)

        types = [e.entity_type for e in entities]
        assert "Document" in types
        assert "Organisation" in types

    @pytest.mark.unit
    def test_authored_by_relationship(self):
        fm = {"title": "My Doc", "source": "Google"}
        entities: list[RawEntity] = []
        rels: list[RawRelationship] = []
        extract_from_frontmatter(fm, "eng/x.md", "software-engineering", entities, rels)

        authored = [r for r in rels if r.kind == "AUTHORED_BY"]
        assert len(authored) == 1
        assert authored[0].to_name == "Google"

    @pytest.mark.unit
    def test_framework_title_detected(self):
        fm = {"title": "Service Design Framework", "source": "X"}
        entities: list[RawEntity] = []
        rels: list[RawRelationship] = []
        extract_from_frontmatter(fm, "eng/x.md", "software-engineering", entities, rels)

        frameworks = [e for e in entities if e.entity_type == "Framework"]
        assert len(frameworks) == 1
        assert frameworks[0].name == "Service Design Framework"


class TestHeadingExtraction:
    @pytest.mark.unit
    def test_framework_pattern_in_heading(self):
        body = "# Introduction\n\n## Agile Delivery Framework\n\nSome text."
        entities: list[RawEntity] = []
        rels: list[RawRelationship] = []
        extract_from_headings(body, "eng/x.md", "software-engineering", "My Doc", entities, rels)

        frameworks = [e for e in entities if e.entity_type == "Framework"]
        assert any("Agile Delivery Framework" in e.name for e in frameworks)

    @pytest.mark.unit
    def test_teaches_relationship(self):
        body = "# Main\n\n## Sub Topic Here\n\nContent."
        entities: list[RawEntity] = []
        rels: list[RawRelationship] = []
        extract_from_headings(body, "eng/x.md", "software-engineering", "My Doc", entities, rels)

        teaches = [r for r in rels if r.kind == "TEACHES"]
        assert len(teaches) >= 1
        assert teaches[0].from_name == "My Doc"


class TestSeedEntities:
    @pytest.mark.unit
    def test_detects_known_person(self):
        text = "The writings of Marcus Aurelius influenced Stoic philosophy."
        entities: list[RawEntity] = []
        rels: list[RawRelationship] = []
        extract_seed_entities(text, "philosophy/x.md", "philosophy", entities, rels)

        people = [e for e in entities if e.entity_type == "Person"]
        assert any(e.name == "Marcus Aurelius" for e in people)

    @pytest.mark.unit
    def test_detects_known_org(self):
        text = "OWASP provides security guidance for web applications."
        entities: list[RawEntity] = []
        rels: list[RawRelationship] = []
        extract_seed_entities(text, "security/x.md", "cybersecurity", entities, rels)

        orgs = [e for e in entities if e.entity_type == "Organisation"]
        assert any(e.name == "OWASP" for e in orgs)

    @pytest.mark.unit
    def test_detects_known_publication(self):
        text = "The Art of War describes military strategy."
        entities: list[RawEntity] = []
        rels: list[RawRelationship] = []
        extract_seed_entities(text, "philosophy/x.md", "philosophy", entities, rels)

        pubs = [e for e in entities if e.entity_type == "Publication"]
        assert any(e.name == "Art of War" for e in pubs)


class TestScanReferenceLibrary:
    @pytest.mark.integration
    def test_scan_with_temp_files(self, tmp_path: Path):
        """Scan a small temporary reference library."""
        col = tmp_path / "engineering" / "test-source"
        col.mkdir(parents=True)
        (col / "doc.md").write_text(
            _make_md(
                "Test Framework Guide",
                "Test Org",
                "engineering",
                "test-source",
                "## OWASP Best Practices\n\nSecurity guidance.\n\n## Risk Model\n\nContent.",
            )
        )

        entities, _rels = scan_reference_library(tmp_path)
        assert len(entities) > 0
        types = {e.entity_type for e in entities}
        assert "Document" in types
        assert "Organisation" in types

    @pytest.mark.integration
    def test_scan_skips_root_files(self, tmp_path: Path):
        """Root-level files like CATALOGUE.md are skipped."""
        (tmp_path / "CATALOGUE.md").write_text("# Catalogue\n\nIgnored.")
        col = tmp_path / "eng" / "src"
        col.mkdir(parents=True)
        (col / "a.md").write_text(_make_md("A", "Src", "eng", "src", "Body"))

        entities, _rels = scan_reference_library(tmp_path)
        names = [e.name for e in entities]
        assert "Catalogue" not in names
