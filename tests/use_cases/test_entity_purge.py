"""Unit tests for ``kairix.use_cases.entity_purge.run_entity_purge`` (#261).

Sabotage-proof anchors:

- ``test_dry_run_does_not_call_cypher`` — drop the ``if dry_run:`` early
  return in ``run_entity_purge`` and the call recorder records a delete.
- ``test_execute_issues_detach_delete_per_row`` — comment out the
  ``neo4j_client.cypher(...)`` call in ``_delete_one`` and the call list
  becomes empty.
- ``test_execute_records_error_for_failing_row_without_aborting`` —
  remove the try/except in ``_delete_one`` and the second row never
  runs (exception propagates).
- ``test_missing_audit_report_returns_error_envelope`` — drop the
  except in ``_load_rows`` and the test raises instead of getting an
  error envelope.
- ``test_execute_without_neo4j_client_returns_error`` — remove the
  ``available`` short-circuit and the executor proceeds with a None
  client and AttributeErrors out.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kairix.use_cases.entity_audit import EntityAuditRow
from kairix.use_cases.entity_purge import (
    EntityPurgeDeps,
    PurgeResult,
    format_purge_json,
    format_purge_text,
    run_entity_purge,
)

pytestmark = pytest.mark.unit


class _RecordingNeo4j:
    """Tiny Neo4j fake that records every ``.cypher()`` call.

    Pass ``raise_for_ids`` to make the executor raise for matching ids.
    """

    def __init__(self, *, raise_for_ids: set[str] | None = None, available: bool = True) -> None:
        self._raise_for_ids = set(raise_for_ids or set())
        self.available = available
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def cypher(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.calls.append((query, params))
        if params and params.get("id") in self._raise_for_ids:
            raise RuntimeError(f"Neo4j refused delete for {params['id']}")
        return []


def _row(
    eid: str,
    *,
    name: str = "X",
    typ: str = "Concept",
    reason: str = "no vault_path and no summary",
) -> EntityAuditRow:
    return EntityAuditRow(id=eid, name=name, type=typ, mode="junk", reason=reason)


def _loader(rows: list[EntityAuditRow]):
    return lambda _path: list(rows)


def test_dry_run_does_not_call_cypher() -> None:
    """Sabotage anchor: drop the ``if dry_run:`` branch — the call recorder grows."""
    neo4j = _RecordingNeo4j()
    rows = [_row("a"), _row("b")]
    deps = EntityPurgeDeps(neo4j_client=neo4j, audit_loader=_loader(rows))
    result = run_entity_purge("/tmp/fake.json", dry_run=True, deps=deps)

    assert result.dry_run is True
    assert result.candidate_count == 2
    assert result.deleted_count == 0
    assert [r.id for r in result.rows] == ["a", "b"]
    assert neo4j.calls == []
    assert "MATCH (n {id: $id}) DETACH DELETE n" in result.cypher
    assert result.error == ""


def test_execute_issues_detach_delete_per_row() -> None:
    """Sabotage anchor: skip the ``.cypher`` call in ``_delete_one`` — calls drops to []."""
    neo4j = _RecordingNeo4j()
    rows = [_row("a"), _row("b")]
    deps = EntityPurgeDeps(neo4j_client=neo4j, audit_loader=_loader(rows))
    result = run_entity_purge("/tmp/fake.json", dry_run=False, deps=deps)

    assert result.dry_run is False
    assert result.candidate_count == 2
    assert result.deleted_count == 2
    assert len(neo4j.calls) == 2
    assert all("DETACH DELETE" in q for q, _ in neo4j.calls)
    assert {p["id"] for _, p in neo4j.calls if p} == {"a", "b"}
    assert all(e.status == "deleted" for e in result.audit_log)


def test_execute_records_error_for_failing_row_without_aborting() -> None:
    """Sabotage anchor: drop the try/except in ``_delete_one`` — the second row never runs."""
    neo4j = _RecordingNeo4j(raise_for_ids={"bad"})
    rows = [_row("good"), _row("bad"), _row("also-good")]
    deps = EntityPurgeDeps(neo4j_client=neo4j, audit_loader=_loader(rows))
    result = run_entity_purge("/tmp/fake.json", dry_run=False, deps=deps)

    assert result.candidate_count == 3
    assert result.deleted_count == 2
    statuses = {entry.id: entry.status for entry in result.audit_log}
    assert statuses["good"] == "deleted"
    assert statuses["also-good"] == "deleted"
    assert statuses["bad"].startswith("error:")
    assert "RuntimeError" in statuses["bad"]


def test_execute_skips_rows_with_blank_id() -> None:
    neo4j = _RecordingNeo4j()
    rows = [_row(""), _row("ok")]
    deps = EntityPurgeDeps(neo4j_client=neo4j, audit_loader=_loader(rows))
    result = run_entity_purge("/tmp/fake.json", dry_run=False, deps=deps)

    assert result.deleted_count == 1
    skipped = [e for e in result.audit_log if e.status.startswith("skipped")]
    assert len(skipped) == 1


def test_missing_audit_report_returns_error_envelope(tmp_path: Path) -> None:
    """Sabotage anchor: removing the loader try/except propagates the FileNotFoundError."""
    deps = EntityPurgeDeps(neo4j_client=_RecordingNeo4j())
    result = run_entity_purge(str(tmp_path / "no-such.json"), dry_run=True, deps=deps)
    assert result.candidate_count == 0
    assert result.error.startswith("FileNotFoundError:")


def test_malformed_audit_report_returns_error_envelope(tmp_path: Path) -> None:
    report = tmp_path / "bad.json"
    report.write_text("not json{", encoding="utf-8")
    deps = EntityPurgeDeps(neo4j_client=_RecordingNeo4j())
    result = run_entity_purge(str(report), dry_run=True, deps=deps)
    # JSONDecodeError is a subclass of ValueError; the loader catches both.
    assert result.error.startswith(("ValueError:", "JSONDecodeError:"))


def test_audit_report_missing_rows_field_returns_error_envelope(tmp_path: Path) -> None:
    report = tmp_path / "norows.json"
    report.write_text(json.dumps({"mode": "all"}), encoding="utf-8")
    deps = EntityPurgeDeps(neo4j_client=_RecordingNeo4j())
    result = run_entity_purge(str(report), dry_run=True, deps=deps)
    assert result.error.startswith("KeyError:")


def test_default_loader_reads_real_json_audit(tmp_path: Path) -> None:
    """The production loader handles the exact shape ``entity_audit.format_report_json`` emits."""
    report = tmp_path / "audit.json"
    payload = {
        "mode": "all",
        "generated_at": "2026-05-14T00:00:00Z",
        "total": 1,
        "rows": [
            {"id": "a", "name": "A", "type": "Person", "mode": "junk", "reason": "no vault_path and no summary"},
        ],
    }
    report.write_text(json.dumps(payload), encoding="utf-8")
    deps = EntityPurgeDeps(neo4j_client=_RecordingNeo4j())
    result = run_entity_purge(str(report), dry_run=True, deps=deps)
    assert result.candidate_count == 1
    assert result.rows[0].id == "a"
    assert result.rows[0].mode == "junk"


def test_execute_without_neo4j_client_returns_error() -> None:
    """Sabotage anchor: remove the ``available`` check and execute proceeds with None."""
    rows = [_row("a")]
    deps = EntityPurgeDeps(neo4j_client=None, audit_loader=_loader(rows))
    result = run_entity_purge("/tmp/fake.json", dry_run=False, deps=deps)
    assert result.error.startswith("Neo4jUnavailable:")
    assert result.deleted_count == 0


def test_execute_with_unavailable_neo4j_returns_error() -> None:
    rows = [_row("a")]
    neo4j = _RecordingNeo4j(available=False)
    deps = EntityPurgeDeps(neo4j_client=neo4j, audit_loader=_loader(rows))
    result = run_entity_purge("/tmp/fake.json", dry_run=False, deps=deps)
    assert result.error.startswith("Neo4jUnavailable:")
    assert neo4j.calls == []


def test_empty_audit_report_dry_run() -> None:
    deps = EntityPurgeDeps(neo4j_client=_RecordingNeo4j(), audit_loader=_loader([]))
    result = run_entity_purge("/tmp/fake.json", dry_run=True, deps=deps)
    assert result.candidate_count == 0
    assert result.deleted_count == 0
    assert result.error == ""


def test_empty_audit_report_execute_returns_zero_deleted() -> None:
    neo4j = _RecordingNeo4j()
    deps = EntityPurgeDeps(neo4j_client=neo4j, audit_loader=_loader([]))
    result = run_entity_purge("/tmp/fake.json", dry_run=False, deps=deps)
    assert result.candidate_count == 0
    assert result.deleted_count == 0
    assert neo4j.calls == []
    assert result.audit_log == []


def test_format_purge_text_dry_run_lists_candidates() -> None:
    rows = [_row("a"), _row("b")]
    result = PurgeResult(dry_run=True, candidate_count=2, rows=rows)
    text = format_purge_text(result)
    assert "DRY-RUN" in text
    assert "candidates=2" in text
    assert "a" in text and "b" in text


def test_format_purge_text_execute_shows_audit_log() -> None:
    from kairix.use_cases.entity_purge import PurgeAuditEntry

    rows = [_row("a")]
    entry = PurgeAuditEntry(id="a", name="X", type="Concept", mode="junk", reason="r", status="deleted")
    result = PurgeResult(dry_run=False, candidate_count=1, deleted_count=1, rows=rows, audit_log=[entry])
    text = format_purge_text(result)
    assert "EXECUTE" in text
    assert "Deleted: 1 / 1" in text
    assert "deleted" in text


def test_format_purge_text_with_error() -> None:
    result = PurgeResult(dry_run=True, error="FileNotFoundError: /tmp/missing.json")
    assert "error:" in format_purge_text(result)


def test_format_purge_json_matches_documented_shape() -> None:
    rows = [_row("a")]
    result = PurgeResult(dry_run=True, candidate_count=1, rows=rows)
    payload = json.loads(format_purge_json(result))
    assert payload["dry_run"] is True
    assert payload["candidate_count"] == 1
    assert payload["deleted_count"] == 0
    assert payload["rows"][0]["id"] == "a"
    assert payload["error"] == ""


def test_format_purge_text_dry_run_empty_rows() -> None:
    result = PurgeResult(dry_run=True, candidate_count=0, rows=[])
    text = format_purge_text(result)
    assert "no candidate rows" in text
