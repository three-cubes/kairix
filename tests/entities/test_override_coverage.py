"""Unit tests for override-coverage stats — closes #263.

Two surfaces under test:

1. :class:`kairix.knowledge.entities.filters.KnownEntityAllowlist` accepts
   an :class:`OverrideMatchCounter`; every word-boundary match increments
   the counter against the override text. Tests construct the filter
   directly and assert on the recorded counts (sabotage: drop the
   ``self._match_counter is not None`` line and at least three of these
   tests fail).
2. :func:`kairix.knowledge.store.crawler.crawl` accepts an
   :class:`EntityOverrides` and writes the coverage summary to a sidecar
   JSON. Tests drive ``crawl()`` against a tmp document root, assert on
   the report's ``override_coverage`` and the sidecar contents (sabotage:
   change ``never_matched = sorted(...)`` to ``list(...)`` and the
   ``never_matched_list_is_sorted`` assertion fails).

All tests use canonical fakes — :class:`FakeNeo4jClient` from
``tests/fixtures/neo4j_mock.py`` and a small in-memory
:class:`EntityOverrides` instance — no ``@patch``, no
``monkeypatch.setenv``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kairix.knowledge.entities.filters import (
    KnownEntityAllowlist,
    OverrideMatchCounter,
)
from kairix.knowledge.entities.overrides import EntityOverrides
from kairix.knowledge.store.crawler import crawl
from tests.fixtures.neo4j_mock import FakeNeo4jClient

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# OverrideMatchCounter + KnownEntityAllowlist
# ---------------------------------------------------------------------------


def test_counter_is_zero_when_no_match() -> None:
    counter = OverrideMatchCounter()
    filt = KnownEntityAllowlist(
        [{"text": "AcmeCorp", "label": "ORG", "source": "allowlist", "confidence": 1.0}],
        match_counter=counter,
    )

    filt.apply([], context="this text has no allowlist terms in it")

    assert counter.get("AcmeCorp") == 0


def test_counter_records_single_match() -> None:
    counter = OverrideMatchCounter()
    filt = KnownEntityAllowlist(
        [{"text": "AcmeCorp", "label": "ORG", "source": "allowlist", "confidence": 1.0}],
        match_counter=counter,
    )

    filt.apply([], context="AcmeCorp announced a new product today.")

    assert counter.get("AcmeCorp") == 1


def test_counter_increments_on_each_apply() -> None:
    """Each call to ``apply`` with a matching context increments by one."""
    counter = OverrideMatchCounter()
    filt = KnownEntityAllowlist(
        [{"text": "AcmeCorp", "label": "ORG", "source": "allowlist", "confidence": 1.0}],
        match_counter=counter,
    )

    filt.apply([], context="AcmeCorp announced a product.")
    filt.apply([], context="AcmeCorp also hired a new VP.")
    filt.apply([], context="No relevant terms here.")

    assert counter.get("AcmeCorp") == 2


def test_counter_records_only_word_boundary_matches() -> None:
    """Mirrors the existing #249 fix — substring hits never increment."""
    counter = OverrideMatchCounter()
    filt = KnownEntityAllowlist(
        [{"text": "BB", "label": "ORG", "source": "allowlist", "confidence": 1.0}],
        match_counter=counter,
    )

    # 'abbey' contains 'bb' as a substring but not as a word boundary token.
    filt.apply([], context="the abbey stood on the hill")

    assert counter.get("BB") == 0


def test_filter_without_counter_does_not_raise() -> None:
    """Existing callers (suggest pipeline) pass no counter — must remain working."""
    filt = KnownEntityAllowlist(
        [{"text": "AcmeCorp", "label": "ORG", "source": "allowlist", "confidence": 1.0}],
    )

    result = filt.apply([], context="AcmeCorp announced a new product today.")

    assert [r["text"] for r in result] == ["AcmeCorp"]


# ---------------------------------------------------------------------------
# crawl() integration — coverage report + sidecar JSON
# ---------------------------------------------------------------------------


def _write_md(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _overrides(*terms: tuple[str, str]) -> EntityOverrides:
    """Build an :class:`EntityOverrides` from ``(text, label)`` tuples."""
    allowlist = [{"text": text, "label": label, "source": "allowlist", "confidence": 1.0} for text, label in terms]
    person = {text for text, label in terms if label == "PERSON"}
    org = {text for text, label in terms if label == "ORG"}
    return EntityOverrides(allowlist=allowlist, person_overrides=person, org_overrides=org)


def test_crawl_records_match_counts_for_overrides_that_fire(tmp_path: Path) -> None:
    _write_md(tmp_path, "01-Projects/note.md", "AcmeCorp delivered the milestone. Globex remained quiet.")
    _write_md(tmp_path, "01-Projects/note-2.md", "Globex announced a partnership with AcmeCorp.")
    coverage_path = tmp_path / "coverage.json"

    overrides = _overrides(("AcmeCorp", "ORG"), ("Globex", "ORG"), ("NeverSeen", "ORG"))

    report = crawl(
        document_root=tmp_path,
        neo4j_client=FakeNeo4jClient(entities=[]),
        dry_run=True,
        overrides=overrides,
        coverage_path=coverage_path,
    )

    assert report.override_coverage is not None
    coverage = report.override_coverage
    assert coverage.total_overrides == 3
    assert coverage.matched == 2
    assert coverage.match_counts == {"AcmeCorp": 2, "Globex": 2}
    assert coverage.never_matched == ["NeverSeen"]


def test_crawl_writes_sidecar_json_with_expected_shape(tmp_path: Path) -> None:
    _write_md(tmp_path, "doc.md", "AcmeCorp is partnering with the regional team.")
    coverage_path = tmp_path / "coverage.json"

    overrides = _overrides(("AcmeCorp", "ORG"), ("dead-term", "ORG"))

    report = crawl(
        document_root=tmp_path,
        neo4j_client=FakeNeo4jClient(entities=[]),
        dry_run=True,
        overrides=overrides,
        coverage_path=coverage_path,
    )

    assert report.override_coverage_path == str(coverage_path)
    assert coverage_path.exists()
    payload = json.loads(coverage_path.read_text(encoding="utf-8"))
    assert set(payload.keys()) == {
        "crawl_started_at",
        "total_overrides",
        "matched",
        "never_matched",
        "match_counts",
    }
    assert payload["total_overrides"] == 2
    assert payload["matched"] == 1
    assert payload["match_counts"] == {"AcmeCorp": 1}
    assert payload["never_matched"] == ["dead-term"]
    # crawl_started_at is an ISO timestamp — must parse round-trip-clean.
    from datetime import datetime

    parsed = datetime.fromisoformat(payload["crawl_started_at"])
    assert parsed.tzinfo is not None, "timestamp must be timezone-aware"


def test_never_matched_list_is_sorted(tmp_path: Path) -> None:
    _write_md(tmp_path, "doc.md", "no relevant terms here at all")
    coverage_path = tmp_path / "coverage.json"

    overrides = _overrides(
        ("zeta-org", "ORG"),
        ("alpha-org", "ORG"),
        ("mu-org", "ORG"),
        ("beta-org", "ORG"),
    )

    report = crawl(
        document_root=tmp_path,
        neo4j_client=FakeNeo4jClient(entities=[]),
        dry_run=True,
        overrides=overrides,
        coverage_path=coverage_path,
    )

    assert report.override_coverage is not None
    assert report.override_coverage.never_matched == ["alpha-org", "beta-org", "mu-org", "zeta-org"]


def test_crawl_without_overrides_emits_no_coverage(tmp_path: Path) -> None:
    """When no overrides are supplied, the report carries no coverage block."""
    _write_md(tmp_path, "doc.md", "AcmeCorp is here.")

    report = crawl(
        document_root=tmp_path,
        neo4j_client=FakeNeo4jClient(entities=[]),
        dry_run=True,
    )

    assert report.override_coverage is None
    assert report.override_coverage_path is None


def test_crawl_with_empty_overrides_emits_no_coverage(tmp_path: Path) -> None:
    """An overrides file with zero entries is equivalent to no overrides at all."""
    _write_md(tmp_path, "doc.md", "irrelevant text")

    report = crawl(
        document_root=tmp_path,
        neo4j_client=FakeNeo4jClient(entities=[]),
        dry_run=True,
        overrides=EntityOverrides(),
    )

    assert report.override_coverage is None


def test_match_counts_increment_across_multiple_files(tmp_path: Path) -> None:
    """Counts accumulate across every .md file in the document root."""
    _write_md(tmp_path, "a.md", "AcmeCorp here.")
    _write_md(tmp_path, "nested/b.md", "AcmeCorp again.")
    _write_md(tmp_path, "deeper/dir/c.md", "and AcmeCorp once more.")
    coverage_path = tmp_path / "coverage.json"

    overrides = _overrides(("AcmeCorp", "ORG"))

    report = crawl(
        document_root=tmp_path,
        neo4j_client=FakeNeo4jClient(entities=[]),
        dry_run=True,
        overrides=overrides,
        coverage_path=coverage_path,
    )

    assert report.override_coverage is not None
    assert report.override_coverage.match_counts == {"AcmeCorp": 3}
    assert report.override_coverage.never_matched == []
