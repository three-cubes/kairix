"""Tests for kairix.knowledge.reflib.resolve — entity resolution and dedup.

Drives every test through the public ``resolve_entities`` surface plus
the public ``slugify`` helper. The internal Levenshtein / similarity
helpers are exercised by the fuzzy-match scenarios; if a branch in those
helpers can't be reached through ``resolve_entities``, it's dead code.
"""

from __future__ import annotations

import pytest

from kairix.knowledge.reflib.extract import RawEntity
from kairix.knowledge.reflib.resolve import resolve_entities
from kairix.utils import slugify

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Slug tests
# ---------------------------------------------------------------------------


class TestToSlug:
    @pytest.mark.unit
    def test_simple_name(self):
        assert slugify("Marcus Aurelius") == "marcus-aurelius"

    @pytest.mark.unit
    def test_acronym(self):
        assert slugify("OWASP") == "owasp"

    @pytest.mark.unit
    def test_special_chars(self):
        assert slugify("dbt Labs (Analytics)") == "dbt-labs-analytics"

    @pytest.mark.unit
    def test_hyphenated(self):
        assert slugify("Twelve-Factor App") == "twelve-factor-app"

    @pytest.mark.unit
    def test_empty(self):
        assert slugify("") == ""


# ---------------------------------------------------------------------------
# Resolution tests
# ---------------------------------------------------------------------------


def _raw(
    name: str,
    etype: str,
    domain: str = "test",
    docs: list[str] | None = None,
    aliases: list[str] | None = None,
    confidence: float = 0.9,
) -> RawEntity:
    return RawEntity(
        name=name,
        entity_type=etype,
        domain=domain,
        domains=[domain],
        source_docs=docs or [f"{domain}/doc.md"],
        aliases=aliases or [],
        confidence=confidence,
    )


class TestResolveEntities:
    @pytest.mark.unit
    def test_exact_dedup(self):
        """Same name+type from different docs merges into one."""
        raw = [
            _raw("OWASP", "Organisation", docs=["sec/a.md"]),
            _raw("OWASP", "Organisation", docs=["sec/b.md"]),
            _raw("OWASP", "Organisation", docs=["sec/c.md"]),
        ]
        resolved = resolve_entities(raw)
        owasp = [e for e in resolved if e.id == "owasp"]
        assert len(owasp) == 1
        assert len(owasp[0].source_docs) == 3

    @pytest.mark.unit
    def test_aliases_merged(self):
        """Entities with different names but same slug merge aliases."""
        raw = [
            _raw(
                "OWASP",
                "Organisation",
                aliases=["Open Web Application Security Project"],
            ),
            _raw("OWASP", "Organisation"),
        ]
        resolved = resolve_entities(raw)
        owasp = [e for e in resolved if e.id == "owasp"]
        assert len(owasp) == 1
        assert "Open Web Application Security Project" in owasp[0].aliases

    @pytest.mark.unit
    def test_different_types_not_merged(self):
        """Same name but different entity types stay separate."""
        raw = [
            _raw("AutoGen", "Technology"),
            _raw("AutoGen", "Framework"),
        ]
        resolved = resolve_entities(raw)
        assert len(resolved) == 2

    @pytest.mark.unit
    def test_fuzzy_match_merges_similar(self):
        """Similar slugs within same type get merged."""
        raw = [
            _raw("OpenTelemetry", "Framework", docs=["a.md"]),
            _raw("Open Telemetry", "Framework", docs=["b.md"]),
        ]
        resolved = resolve_entities(raw)
        frameworks = [e for e in resolved if e.entity_type == "Framework"]
        # Should merge because slugs are very similar
        assert len(frameworks) == 1

    @pytest.mark.unit
    def test_dissimilar_slugs_do_not_merge(self):
        """Names that aren't fuzzy-similar stay as separate entities — pins
        the upper edge of the fuzzy-match contract so a regression that
        merges everything would fail loud."""
        raw = [
            _raw("PostgreSQL", "Technology"),
            _raw("MySQL", "Technology"),
            _raw("MongoDB", "Technology"),
        ]
        resolved = resolve_entities(raw)
        techs = [e for e in resolved if e.entity_type == "Technology"]
        assert len(techs) == 3, f"expected 3 distinct techs, got {[e.id for e in techs]}"

    @pytest.mark.unit
    def test_confidence_preserved(self):
        """The highest confidence score is kept."""
        raw = [
            _raw("Google", "Organisation", confidence=0.7),
            _raw("Google", "Organisation", confidence=0.95),
        ]
        resolved = resolve_entities(raw)
        google = [e for e in resolved if e.id == "google"]
        assert google[0].confidence == pytest.approx(0.95)

    @pytest.mark.unit
    def test_empty_names_skipped(self):
        """Entities with empty names are dropped."""
        raw = [
            _raw("", "Organisation"),
            _raw("  ", "Organisation"),
        ]
        resolved = resolve_entities(raw)
        assert len(resolved) == 0

    @pytest.mark.unit
    def test_domains_merged_across_collections(self):
        """Domains from different source collections are merged."""
        raw = [
            _raw(
                "Microsoft",
                "Organisation",
                domain="technology",
                docs=["agentic-ai/a.md"],
            ),
            _raw(
                "Microsoft",
                "Organisation",
                domain="software-engineering",
                docs=["eng/b.md"],
            ),
        ]
        resolved = resolve_entities(raw)
        ms = [e for e in resolved if e.id == "microsoft"]
        assert len(ms) == 1
        assert "technology" in ms[0].domains
        assert "software-engineering" in ms[0].domains
