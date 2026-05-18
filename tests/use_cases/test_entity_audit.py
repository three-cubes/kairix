"""Unit tests for ``kairix.use_cases.entity_audit.run_entity_audit`` (#260).

Sabotage-proof anchors:

- ``test_junk_mode_returns_entities_missing_vault_path_and_summary`` —
  swap the Cypher ``AND`` for ``OR`` in ``_audit_junk`` and this test
  flips on the "non-junk" entity that has a summary.
- ``test_paths_mode_returns_only_entities_with_missing_vault_files`` —
  invert the ``if deps.path_exists(...)`` short-circuit in
  ``_audit_paths`` and the present-file entity flips into the result.
- ``test_enrichment_mode_flags_missing_fields`` — drop the ``summary``
  predicate from ``_enrichment_reason`` and the assertion on the
  ``missing summary`` reason fails.
- ``test_all_mode_deduplicates_by_id`` — remove the ``seen`` set from
  ``_audit_all`` and the same id appears twice in the row list.
- ``test_run_entity_audit_swallows_cypher_failure`` — remove the
  try/except in ``_query_rows`` and the test raises instead of
  returning an empty list.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from kairix.use_cases.entity_audit import (
    AuditReport,
    EntityAuditDeps,
    EntityAuditMode,
    EntityAuditRow,
    format_report_json,
    format_report_text,
    run_entity_audit,
)

pytestmark = pytest.mark.unit


class _ScriptedNeo4j:
    """Tiny scripted Neo4j fake.

    Pattern-matches a fragment of the Cypher string against the
    ``rows_by_fragment`` mapping. The fake checks fragments **in
    insertion order** and returns the first match — pass the most
    specific fragment first when two queries share a token.

    Suggested fragments (one per audit lens):

    - junk:        ``"AND (n.vault_path IS NULL"``  (junk has both predicates)
    - paths:       ``"n.vault_path IS NOT NULL"``
    - enrichment:  ``"n.wikidata_qid"``
    """

    def __init__(
        self,
        *,
        rows_by_fragment: dict[str, list[dict[str, Any]]] | None = None,
        available: bool = True,
        raises: bool = False,
    ) -> None:
        self._rows_by_fragment = dict(rows_by_fragment or {})
        self.available = available
        self._raises = raises
        self.calls: list[str] = []

    def cypher(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        del params
        self.calls.append(query)
        if self._raises:
            raise RuntimeError("Neo4j down")
        for fragment, rows in self._rows_by_fragment.items():
            if fragment in query:
                return list(rows)
        return []


def _fixed_now() -> str:
    return "2026-05-14T00:00:00Z"


def test_run_entity_audit_returns_audit_report_with_mode_and_timestamp() -> None:
    deps = EntityAuditDeps(neo4j_client=_ScriptedNeo4j(), now_fn=_fixed_now)
    report = run_entity_audit(EntityAuditMode.JUNK, deps=deps)
    assert isinstance(report, AuditReport)
    assert report.mode == "junk"
    assert report.generated_at == "2026-05-14T00:00:00Z"
    assert report.total == 0


def test_junk_mode_returns_entities_missing_vault_path_and_summary() -> None:
    """Sabotage anchor: junk query must require both vault_path AND summary missing."""
    neo4j = _ScriptedNeo4j(
        rows_by_fragment={
            # Junk query — only the missing-both rows make it past the Cypher predicate.
            # The fake only returns what the scripted rows say, so we model the production behaviour.
            "AND (n.vault_path IS NULL": [
                {"id": "ghost-1", "name": "Ghost One", "label": "Concept"},
            ],
        }
    )
    deps = EntityAuditDeps(neo4j_client=neo4j, now_fn=_fixed_now)
    report = run_entity_audit(EntityAuditMode.JUNK, deps=deps)
    assert report.total == 1
    row = report.rows[0]
    assert row.id == "ghost-1"
    assert row.name == "Ghost One"
    assert row.type == "Concept"
    assert row.mode == "junk"
    assert "no vault_path and no summary" in row.reason


def test_junk_mode_handles_empty_result() -> None:
    deps = EntityAuditDeps(neo4j_client=_ScriptedNeo4j(), now_fn=_fixed_now)
    report = run_entity_audit(EntityAuditMode.JUNK, deps=deps)
    assert report.rows == []


def test_paths_mode_returns_only_entities_with_missing_vault_files() -> None:
    """Sabotage anchor: paths audit must skip entities whose file still exists."""
    neo4j = _ScriptedNeo4j(
        rows_by_fragment={
            # Paths query — distinguished by the "vault_path IS NOT NULL" predicate.
            "n.vault_path IS NOT NULL": [
                {"id": "still-here", "name": "Still Here", "label": "Person", "vault_path": "people/here.md"},
                {"id": "deleted", "name": "Deleted", "label": "Person", "vault_path": "people/gone.md"},
            ],
        }
    )
    # path_exists: only the first row's path exists.
    fs: dict[str, bool] = {"people/here.md": True, "people/gone.md": False}
    deps = EntityAuditDeps(
        neo4j_client=neo4j,
        path_exists=lambda p: fs.get(p, False),
        now_fn=_fixed_now,
    )
    report = run_entity_audit(EntityAuditMode.PATHS, deps=deps)
    assert [r.id for r in report.rows] == ["deleted"]
    assert "people/gone.md" in report.rows[0].reason


def test_paths_mode_respects_document_root_prefix() -> None:
    """When ``document_root`` is set the path probe sees the joined path."""
    neo4j = _ScriptedNeo4j(
        rows_by_fragment={
            "n.vault_path IS NOT NULL": [
                {"id": "x", "name": "X", "label": "Concept", "vault_path": "entities/x.md"},
            ],
        }
    )
    seen: list[str] = []

    def _probe(p: str) -> bool:
        seen.append(p)
        return True

    deps = EntityAuditDeps(
        neo4j_client=neo4j,
        path_exists=_probe,
        document_root="/data/vault",
        now_fn=_fixed_now,
    )
    run_entity_audit(EntityAuditMode.PATHS, deps=deps)
    assert seen == ["/data/vault/entities/x.md"]


def test_paths_mode_skips_rows_with_empty_vault_path() -> None:
    """Rows that slipped through the Cypher filter with a blank vault_path are skipped."""
    neo4j = _ScriptedNeo4j(
        rows_by_fragment={
            "n.vault_path IS NOT NULL": [
                {"id": "x", "name": "X", "label": "Concept", "vault_path": ""},
            ],
        }
    )
    deps = EntityAuditDeps(
        neo4j_client=neo4j,
        path_exists=lambda _p: False,
        now_fn=_fixed_now,
    )
    report = run_entity_audit(EntityAuditMode.PATHS, deps=deps)
    assert report.rows == []


def test_enrichment_mode_flags_missing_fields() -> None:
    """Sabotage anchor: removing the summary predicate from ``_enrichment_reason``
    causes the missing-summary row to be silently dropped."""
    neo4j = _ScriptedNeo4j(
        rows_by_fragment={
            # Enrichment query — distinguished by the wikidata_qid token.
            "n.wikidata_qid": [
                {
                    "id": "no-summary",
                    "name": "No Summary",
                    "label": "Person",
                    "summary": "",
                    "wikidata_qid": "Q42",
                },
                {
                    "id": "no-qid",
                    "name": "No QID",
                    "label": "Person",
                    "summary": "person",
                    "wikidata_qid": "",
                },
                {
                    "id": "fully-enriched",
                    "name": "Enriched",
                    "label": "Person",
                    "summary": "person",
                    "wikidata_qid": "Q1",
                },
            ],
        }
    )
    deps = EntityAuditDeps(neo4j_client=neo4j, now_fn=_fixed_now)
    report = run_entity_audit(EntityAuditMode.ENRICHMENT, deps=deps)
    ids = {r.id for r in report.rows}
    assert ids == {"no-summary", "no-qid"}
    by_id = {r.id: r for r in report.rows}
    assert "summary" in by_id["no-summary"].reason
    assert "wikidata_qid" in by_id["no-qid"].reason


def test_enrichment_mode_flags_missing_label() -> None:
    neo4j = _ScriptedNeo4j(
        rows_by_fragment={
            "n.wikidata_qid": [
                {"id": "unlabeled", "name": "U", "label": "", "summary": "s", "wikidata_qid": "Q1"},
            ],
        }
    )
    deps = EntityAuditDeps(neo4j_client=neo4j, now_fn=_fixed_now)
    report = run_entity_audit(EntityAuditMode.ENRICHMENT, deps=deps)
    assert report.rows[0].id == "unlabeled"
    assert "label" in report.rows[0].reason


def test_all_mode_deduplicates_by_id() -> None:
    """Sabotage anchor: dropping the ``seen`` set yields a duplicated row."""
    shared = {"id": "double", "name": "Double", "label": "Concept"}
    neo4j = _ScriptedNeo4j(
        rows_by_fragment={
            "AND (n.vault_path IS NULL": [shared],
            "n.wikidata_qid": [
                {"id": "double", "name": "Double", "label": "Concept", "summary": "", "wikidata_qid": ""},
            ],
        }
    )
    deps = EntityAuditDeps(neo4j_client=neo4j, now_fn=_fixed_now)
    report = run_entity_audit(EntityAuditMode.ALL, deps=deps)
    assert [r.id for r in report.rows] == ["double"]


def test_all_mode_combines_distinct_rows_from_each_lens() -> None:
    neo4j = _ScriptedNeo4j(
        rows_by_fragment={
            "AND (n.vault_path IS NULL": [
                {"id": "junker", "name": "Junker", "label": "Concept"},
            ],
            "n.wikidata_qid": [
                {
                    "id": "enricher",
                    "name": "Enricher",
                    "label": "Person",
                    "summary": "",
                    "wikidata_qid": "",
                },
            ],
        }
    )
    deps = EntityAuditDeps(neo4j_client=neo4j, now_fn=_fixed_now)
    report = run_entity_audit(EntityAuditMode.ALL, deps=deps)
    assert {r.id for r in report.rows} == {"junker", "enricher"}


def test_empty_graph_returns_empty_report_for_every_mode() -> None:
    deps = EntityAuditDeps(neo4j_client=_ScriptedNeo4j(), now_fn=_fixed_now)
    for mode in EntityAuditMode:
        report = run_entity_audit(mode, deps=deps)
        assert report.total == 0
        assert report.mode == mode.value


def test_run_entity_audit_swallows_cypher_failure() -> None:
    """Sabotage anchor: ``_query_rows`` must catch — remove the try/except
    and this test raises instead of producing an empty report."""
    deps = EntityAuditDeps(neo4j_client=_ScriptedNeo4j(raises=True), now_fn=_fixed_now)
    report = run_entity_audit(EntityAuditMode.ALL, deps=deps)
    assert report.total == 0


def test_run_entity_audit_handles_no_client() -> None:
    deps = EntityAuditDeps(neo4j_client=None, now_fn=_fixed_now)
    report = run_entity_audit(EntityAuditMode.ALL, deps=deps)
    assert report.total == 0


def test_run_entity_audit_handles_unavailable_client() -> None:
    deps = EntityAuditDeps(neo4j_client=_ScriptedNeo4j(available=False), now_fn=_fixed_now)
    report = run_entity_audit(EntityAuditMode.ALL, deps=deps)
    assert report.total == 0


def test_rows_without_id_and_name_are_skipped() -> None:
    neo4j = _ScriptedNeo4j(
        rows_by_fragment={
            "AND (n.vault_path IS NULL": [
                {"id": None, "name": None, "label": "Concept"},
                {"id": "ok", "name": "OK", "label": "Concept"},
            ],
        }
    )
    deps = EntityAuditDeps(neo4j_client=neo4j, now_fn=_fixed_now)
    report = run_entity_audit(EntityAuditMode.JUNK, deps=deps)
    assert [r.id for r in report.rows] == ["ok"]


def test_default_deps_uses_no_real_filesystem_when_neo4j_is_none() -> None:
    """Default constructor must be safe — no neo4j, no path I/O."""
    report = run_entity_audit(EntityAuditMode.PATHS)
    assert report.total == 0


def test_format_report_json_matches_documented_shape() -> None:
    row = EntityAuditRow(id="a", name="A", type="Person", mode="junk", reason="no vault_path and no summary")
    report = AuditReport(mode="all", generated_at="2026-05-14T00:00:00Z", rows=[row])
    payload = json.loads(format_report_json(report))
    assert payload["mode"] == "all"
    assert payload["generated_at"] == "2026-05-14T00:00:00Z"
    assert payload["total"] == 1
    assert payload["rows"] == [
        {
            "id": "a",
            "name": "A",
            "type": "Person",
            "mode": "junk",
            "reason": "no vault_path and no summary",
        }
    ]


def test_format_report_text_renders_rows_when_present() -> None:
    row = EntityAuditRow(id="a", name="A", type="Person", mode="junk", reason="no vault_path and no summary")
    report = AuditReport(mode="all", generated_at="2026-05-14T00:00:00Z", rows=[row])
    text = format_report_text(report)
    assert "ID" in text and "NAME" in text and "TYPE" in text
    assert "a" in text and "A" in text and "Person" in text
    assert "junk" in text and "no vault_path and no summary" in text


def test_format_report_text_empty_report() -> None:
    report = AuditReport(mode="all", generated_at="2026-05-14T00:00:00Z", rows=[])
    text = format_report_text(report)
    assert "No audit rows found" in text
