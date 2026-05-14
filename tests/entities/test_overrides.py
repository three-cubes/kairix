"""Unit tests for ``kairix.knowledge.entities.overrides`` — vault-driven
entity-overrides loader (closes #166).

The loader resolves the file path, parses the markdown list format,
and returns the three containers the filter chain accepts. Tests drive
the parser directly via :func:`load_entity_overrides` with an explicit
``Path`` so no env-var monkeypatching is required (F2-clean).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kairix.knowledge.entities.overrides import (
    EntityOverrides,
    load_entity_overrides,
)

pytestmark = pytest.mark.unit


def _write(tmp_path: Path, body: str) -> Path:
    """Write ``body`` to ``tmp_path/_entity-overrides.md`` and return the path."""
    p = tmp_path / "_entity-overrides.md"
    p.write_text(body, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Happy path — single entry per label
# ---------------------------------------------------------------------------


def test_single_org_entry_populates_allowlist_and_org_override(tmp_path: Path) -> None:
    """One ``ORG`` entry produces one allowlist row and one org_override."""
    p = _write(tmp_path, '# Entity Overrides\n\n- "YYY": ORG\n')

    overrides = load_entity_overrides(p)

    assert overrides.allowlist == [{"text": "YYY", "label": "ORG", "source": "allowlist", "confidence": 1.0}]
    assert overrides.org_overrides == {"YYY"}
    assert overrides.person_overrides == set()


def test_single_person_entry_populates_allowlist_and_person_override(tmp_path: Path) -> None:
    """One ``PERSON`` entry produces one allowlist row and one person_override."""
    p = _write(tmp_path, '- "Jane Doe": PERSON\n')

    overrides = load_entity_overrides(p)

    assert overrides.person_overrides == {"Jane Doe"}
    assert overrides.org_overrides == set()
    assert overrides.allowlist[0]["label"] == "PERSON"


def test_multiple_entries_all_loaded(tmp_path: Path) -> None:
    """All entries from a multi-line file are parsed; order is preserved
    for the allowlist and the override sets accumulate."""
    body = '# Entity Overrides\n\n- "YYY": ORG\n- "AAA": ORG\n- "BBB": ORG\n- "CCC": ORG\n- "ZZZ": PERSON\n'
    p = _write(tmp_path, body)

    overrides = load_entity_overrides(p)

    texts = [row["text"] for row in overrides.allowlist]
    assert texts == ["YYY", "AAA", "BBB", "CCC", "ZZZ"]
    assert overrides.org_overrides == {"YYY", "AAA", "BBB", "CCC"}
    assert overrides.person_overrides == {"ZZZ"}


# ---------------------------------------------------------------------------
# Missing / malformed file — must not raise
# ---------------------------------------------------------------------------


def test_missing_file_returns_empty_overrides(tmp_path: Path) -> None:
    """A non-existent path returns empty containers — never raises."""
    overrides = load_entity_overrides(tmp_path / "does-not-exist.md")
    assert overrides == EntityOverrides()


def test_none_path_returns_empty_overrides() -> None:
    """``path=None`` short-circuits and returns empty containers."""
    assert load_entity_overrides(None) == EntityOverrides()


def test_malformed_entries_logged_and_skipped(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A list item that doesn't match the grammar logs a warning and is
    skipped — well-formed entries on other lines still load."""
    body = '# Entity Overrides\n\n- this line is malformed and missing quotes\n- "GoodOne": ORG\n'
    p = _write(tmp_path, body)

    with caplog.at_level("WARNING", logger="kairix.knowledge.entities.overrides"):
        overrides = load_entity_overrides(p)

    assert overrides.org_overrides == {"GoodOne"}
    assert any("unparseable entry" in rec.message for rec in caplog.records)


def test_unknown_label_logged_and_skipped(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Entries with a label outside the spaCy entity vocabulary log a
    warning and are dropped — they never reach the filter chain."""
    body = '- "BogusEntry": SOMETHINGELSE\n- "Acme": ORG\n'
    p = _write(tmp_path, body)

    with caplog.at_level("WARNING", logger="kairix.knowledge.entities.overrides"):
        overrides = load_entity_overrides(p)

    assert overrides.org_overrides == {"Acme"}
    assert "BogusEntry" not in {row["text"] for row in overrides.allowlist}
    assert any("unknown label" in rec.message for rec in caplog.records)


def test_blank_lines_and_headings_ignored(tmp_path: Path) -> None:
    """Markdown headings, prose, and blank lines don't generate entries."""
    body = '# Heading\n\nSome prose about the file format.\n\n- "RealOrg": ORG\n\n## Another heading\n'
    p = _write(tmp_path, body)

    overrides = load_entity_overrides(p)
    assert overrides.org_overrides == {"RealOrg"}


# ---------------------------------------------------------------------------
# case_insensitive flag — expands surface forms
# ---------------------------------------------------------------------------


def test_case_insensitive_flag_expands_term_variants(tmp_path: Path) -> None:
    """``case_insensitive: true`` registers upper-, lower- and title-cased
    variants so any input casing wins the filter-chain lookup."""
    p = _write(tmp_path, '- "yyy": ORG, case_insensitive: true\n')

    overrides = load_entity_overrides(p)

    # All three variants in org_overrides (set semantics).
    assert "yyy" in overrides.org_overrides
    assert "YYY" in overrides.org_overrides
    assert "Yyy" in overrides.org_overrides


def test_case_sensitive_default_keeps_only_exact_term(tmp_path: Path) -> None:
    """Without the flag, only the literal as-written term registers."""
    p = _write(tmp_path, '- "YYY": ORG\n')

    overrides = load_entity_overrides(p)

    assert overrides.org_overrides == {"YYY"}
    # Regression guard: lower-case must not sneak in.
    assert "yyy" not in overrides.org_overrides


def test_unknown_flag_logged_and_ignored(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Future-proof: an unrecognised flag logs a warning but the entry
    still loads with default semantics."""
    p = _write(tmp_path, '- "Future": ORG, fancy_flag: true\n')

    with caplog.at_level("WARNING", logger="kairix.knowledge.entities.overrides"):
        overrides = load_entity_overrides(p)

    assert overrides.org_overrides == {"Future"}
    assert any("unknown flag" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Path read failure — must not raise
# ---------------------------------------------------------------------------


def test_unreadable_file_returns_empty_with_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """If ``read_text`` raises (e.g. permission denied), the loader logs
    and returns empty — entity suggest must never block on a vault read."""

    class _ExplodingPath:
        """Path-shaped stub: exists() is True, read_text() raises OSError."""

        def exists(self) -> bool:
            return True

        def read_text(self, encoding: str = "utf-8") -> str:
            raise OSError("permission denied")

        def __str__(self) -> str:
            return "/fake/overrides.md"

    with caplog.at_level("WARNING", logger="kairix.knowledge.entities.overrides"):
        overrides = load_entity_overrides(_ExplodingPath())  # type: ignore[arg-type]  # duck-typed Path stub for the read-failure branch

    assert overrides == EntityOverrides()
    assert any("cannot read" in rec.message for rec in caplog.records)


def test_unstattable_file_returns_empty_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If ``exists()`` raises OSError, the loader logs and returns empty."""

    class _UnstattablePath:
        def exists(self) -> bool:
            raise OSError("filesystem gone")

        def __str__(self) -> str:
            return "/fake/overrides.md"

    with caplog.at_level("WARNING", logger="kairix.knowledge.entities.overrides"):
        overrides = load_entity_overrides(_UnstattablePath())  # type: ignore[arg-type]  # duck-typed Path stub for the stat-failure branch

    assert overrides == EntityOverrides()
    assert any("cannot stat" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Empty term / empty label edge cases
# ---------------------------------------------------------------------------


def test_empty_term_quoted_skipped(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """An empty quoted string is treated as a malformed entry."""
    p = _write(tmp_path, '- "": ORG\n- "RealOrg": ORG\n')

    with caplog.at_level("WARNING", logger="kairix.knowledge.entities.overrides"):
        overrides = load_entity_overrides(p)

    # Only RealOrg loaded; the empty-quote entry doesn't match the regex
    # (which requires at least one character between the quotes).
    assert overrides.org_overrides == {"RealOrg"}


# ---------------------------------------------------------------------------
# Regex-split regression — the two-stage head + tail grammar must match
# everything the old single-regex grammar matched and reject everything it
# rejected. These tests anchor the refactor that split ``_ENTRY_PATTERN``
# into ``_ENTRY_HEAD_PATTERN`` + ``_ENTRY_TAIL_PATTERN`` (Sonar PR #247).
# ---------------------------------------------------------------------------


def test_garbage_tail_after_label_rejected(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Trailing garbage that isn't a flag list is rejected as malformed.

    Sabotage anchor: removing ``_ENTRY_TAIL_PATTERN.match(...)`` from
    ``_parse_entry`` would let ``Acme`` slip through with the garbage tail
    ignored — this test catches that regression by asserting the entry
    is dropped.
    """
    p = _write(tmp_path, '- "Acme": ORG this is not a flag list\n- "Real": ORG\n')

    with caplog.at_level("WARNING", logger="kairix.knowledge.entities.overrides"):
        overrides = load_entity_overrides(p)

    # Acme rejected by the tail-validator; only Real survives.
    assert overrides.org_overrides == {"Real"}
    assert any("unparseable entry" in rec.message for rec in caplog.records)


def test_multiple_flags_in_tail_all_parsed(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Multiple comma-separated flags in the tail are all parsed.

    Anchors the regex split: ``_ENTRY_HEAD_PATTERN`` captures the whole
    tail and ``_FLAG_PATTERN.finditer`` walks it. Two flags must both
    be honoured (case_insensitive fires the expansion; unknown_flag
    logs and is dropped).
    """
    p = _write(tmp_path, '- "xyz": ORG, case_insensitive: true, future_flag: yes\n')

    with caplog.at_level("WARNING", logger="kairix.knowledge.entities.overrides"):
        overrides = load_entity_overrides(p)

    # case_insensitive expansion happened — three surface forms register.
    assert {"xyz", "XYZ", "Xyz"}.issubset(overrides.org_overrides)
    # future_flag was acknowledged via warning, not silently ignored.
    assert any("unknown flag" in rec.message and "future_flag" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Integration with the filter chain — overrides reach suggest_entities
# ---------------------------------------------------------------------------


def test_overrides_promote_missing_org_through_suggest_entities(tmp_path: Path) -> None:
    """End-to-end through the public surface: an override file with YYY
    promotes it as an ORG when the NER model didn't find it.

    Sabotage-prove: with the override file present, YYY appears; with
    the file absent, the test mutation below confirms the assertion
    flips.
    """
    from kairix.knowledge.entities.filters import default_suggestion_filter_chain
    from kairix.knowledge.entities.suggest import suggest_entities
    from tests.entities.test_suggest_filter_integration import _doc_with_entities, _FakeNlp
    from tests.fixtures.neo4j_mock import FakeNeo4jClient

    p = _write(tmp_path, '- "YYY": ORG\n- "AAA": ORG\n')
    overrides = load_entity_overrides(p)
    chain = default_suggestion_filter_chain(
        allowlist=overrides.allowlist,
        person_overrides=overrides.person_overrides,
        org_overrides=overrides.org_overrides,
    )

    text = "ZZZ spoke with the regional lead at YYY about AAA"
    # NER catches ZZZ (PERSON) but misses YYY and AAA — the exact dogfood gap.
    nlp = _FakeNlp(_doc_with_entities(text, [("ZZZ", "PERSON")]))
    neo4j = FakeNeo4jClient(entities=[])

    result = suggest_entities(text, neo4j, nlp=nlp, filter_chain=chain)

    surface = {(r.text, r.label) for r in result}
    assert ("YYY", "ORG") in surface
    assert ("AAA", "ORG") in surface
    assert ("ZZZ", "PERSON") in surface


def test_overrides_correct_mistyped_label_through_suggest_entities(tmp_path: Path) -> None:
    """When NER mistypes a known org as PERSON, the override forces ORG."""
    from kairix.knowledge.entities.filters import default_suggestion_filter_chain
    from kairix.knowledge.entities.suggest import suggest_entities
    from tests.entities.test_suggest_filter_integration import _doc_with_entities, _FakeNlp
    from tests.fixtures.neo4j_mock import FakeNeo4jClient

    p = _write(tmp_path, '- "YYY": ORG\n')
    overrides = load_entity_overrides(p)
    chain = default_suggestion_filter_chain(
        allowlist=overrides.allowlist,
        person_overrides=overrides.person_overrides,
        org_overrides=overrides.org_overrides,
    )

    text = "YYY announced a new initiative."
    # NER mistypes YYY as PERSON — the bug.
    nlp = _FakeNlp(_doc_with_entities(text, [("YYY", "PERSON")]))
    neo4j = FakeNeo4jClient(entities=[])

    result = suggest_entities(text, neo4j, nlp=nlp, filter_chain=chain)

    # The single YYY hit was relabelled — NerLabelFilter wins.
    yyy_hits = [r for r in result if r.text == "YYY"]
    assert yyy_hits, f"expected YYY in {[r.text for r in result]}"
    assert all(r.label == "ORG" for r in yyy_hits)


def test_term_in_override_but_not_in_input_does_not_appear(tmp_path: Path) -> None:
    """Sabotage guard: an override file with BBB doesn't inject BBB when
    the input doesn't mention it (allowlist's substring-match rule)."""
    from kairix.knowledge.entities.filters import default_suggestion_filter_chain
    from kairix.knowledge.entities.suggest import suggest_entities
    from tests.entities.test_suggest_filter_integration import _doc_with_entities, _FakeNlp
    from tests.fixtures.neo4j_mock import FakeNeo4jClient

    p = _write(tmp_path, '- "BBB": ORG\n')
    overrides = load_entity_overrides(p)
    chain = default_suggestion_filter_chain(
        allowlist=overrides.allowlist,
        person_overrides=overrides.person_overrides,
        org_overrides=overrides.org_overrides,
    )

    text = "ZZZ spoke at the AGM"  # BBB intentionally absent
    nlp = _FakeNlp(_doc_with_entities(text, [("ZZZ", "PERSON")]))
    neo4j = FakeNeo4jClient(entities=[])

    result = suggest_entities(text, neo4j, nlp=nlp, filter_chain=chain)

    surface_forms = {r.text for r in result}
    assert "BBB" not in surface_forms


# NOTE: ``entity_overrides_path()`` is covered in ``tests/test_paths.py``
# (the paths-module test file is the single grandfathered home for F2 env
# reads, and adding the path-resolution tests there avoids spreading the
# env-monkeypatch baseline).
