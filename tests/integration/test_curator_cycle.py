"""End-to-end integration tests for the Curator health-check cycle.

Wires ``run_health_check`` against a writable ``FakeNeo4jClient`` so
the full Cypher → row-to-issue → report-aggregation path runs as a
single cycle. The text/JSON formatters are invoked too, since they
are the operator-visible surface and unit tests stub each section
individually.

What's covered here that unit + BDD don't catch:
  - A single ``run_health_check`` call sees a mix of stale + fresh +
    synthesis-missing entities and routes each into the correct
    issues bucket (the unit tests configure one bucket at a time).
  - The cycle's text + JSON renders carry the stale entity ids by name.
  - The report's ``ok`` invariant flips correctly when issues exist.
  - Re-running the cycle against an unchanged graph produces the
    same shape (idempotent — health-check is read-only).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from kairix.agents.curator.health import (
    format_report_json,
    format_report_text,
    run_health_check,
)
from tests.fixtures.neo4j_mock import FakeNeo4jClient

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Boundary fake — extends FakeNeo4jClient to surface health-check fixtures
# ---------------------------------------------------------------------------


class _HealthFakeNeo4jClient(FakeNeo4jClient):
    """FakeNeo4jClient that returns scripted rows for the health-check
    Cypher patterns (counts / synthesis_failures / missing_vault / stale).

    The canonical FakeNeo4jClient pattern-matches on ``"vault_path IS
    NULL"``, ``"summary IS NULL"`` etc. and returns ``[]`` by default —
    the curator health-check needs each pattern to yield distinct rows.
    """

    def __init__(
        self,
        *,
        counts: list[dict[str, Any]] | None = None,
        synthesis_failures: list[dict[str, Any]] | None = None,
        missing_vault_path: list[dict[str, Any]] | None = None,
        stale_entities: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(entities=[])
        self._counts = list(counts or [])
        self._synthesis_failures = list(synthesis_failures or [])
        self._missing_vault_path = list(missing_vault_path or [])
        self._stale_entities = list(stale_entities or [])
        self.cypher_calls: list[tuple[str, dict[str, Any] | None]] = []

    def cypher(self, query: str, params: dict | None = None) -> list[dict]:
        self.cypher_calls.append((query, params))
        if "COUNT(*)" in query:
            return list(self._counts)
        if "summary IS" in query:
            return list(self._synthesis_failures)
        if "vault_path IS" in query:
            return list(self._missing_vault_path)
        if "last_seen" in query:
            return list(self._stale_entities)
        return []


def _entity_row(
    entity_id: str,
    name: str,
    label: str = "Person",
    *,
    last_seen: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {"id": entity_id, "name": name, "label": label}
    if last_seen is not None:
        row["last_seen"] = last_seen
    return row


# ---------------------------------------------------------------------------
# Cycle: mixed graph → report names every issue
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_cycle_reports_stale_and_synthesis_and_missing_path_in_one_run() -> None:
    """A single cycle sees a graph with stale + synthesis-missing + missing-vault
    entities and reports each in the correct issues bucket.

    Sabotage: if the health module's ``_query_entity_issues`` stopped
    feeding the ``"summary IS"`` branch into ``synthesis_failures``, the
    bucket would be empty and the count assertion would fail.
    """
    client = _HealthFakeNeo4jClient(
        counts=[
            {"label": "Person", "cnt": 3},
            {"label": "Organisation", "cnt": 2},
        ],
        synthesis_failures=[_entity_row("ent-1", "Alice", "Person")],
        missing_vault_path=[_entity_row("ent-2", "Bob", "Person")],
        stale_entities=[
            _entity_row("ent-3", "Acme Stale", "Organisation", last_seen="2025-01-01T00:00:00Z"),
            _entity_row("ent-4", "Globex Stale", "Organisation", last_seen="2025-02-01T00:00:00Z"),
        ],
    )

    report = run_health_check(client, staleness_days=90)

    assert report.neo4j_available is True
    assert report.total_entities == 5
    assert report.entities_by_type == {"person": 3, "organisation": 2}
    assert [i.entity_id for i in report.synthesis_failures] == ["ent-1"]
    assert [i.entity_id for i in report.missing_vault_path] == ["ent-2"]
    assert sorted(i.entity_id for i in report.stale_entities) == ["ent-3", "ent-4"]
    assert report.ok is False
    assert report.issue_count == 4


@pytest.mark.integration
def test_cycle_text_and_json_renders_carry_stale_names() -> None:
    """The cycle's formatted output surfaces the stale entity ids by name
    so curators can act without a second query.

    Sabotage: if ``format_report_text`` dropped the stale-issues
    section, the name lookup would fail and the assert would flag it.
    """
    client = _HealthFakeNeo4jClient(
        counts=[{"label": "Organisation", "cnt": 1}],
        stale_entities=[_entity_row("ent-3", "Acme Stale", "Organisation", last_seen="2025-01-01T00:00:00Z")],
    )

    report = run_health_check(client, staleness_days=90)

    text = format_report_text(report)
    payload = json.loads(format_report_json(report))

    # Text output names the stale entity by id (the operator-readable line).
    assert "ent-3" in text
    assert "ISSUES FOUND" in text
    # JSON payload carries the same row in structured form.
    stale_ids = [s["entity_id"] for s in payload["stale_entities"]]
    assert stale_ids == ["ent-3"]
    # The derived ``ok`` field is included for green/red gating.
    assert payload["ok"] is False
    assert payload["issue_count"] == 1


@pytest.mark.integration
def test_cycle_idempotent_on_clean_graph() -> None:
    """Running the cycle twice against an unchanged graph produces an
    identical, empty-issue report each time.

    Sabotage: if ``run_health_check`` mutated client state between
    invocations (e.g. cached row results), the second cycle would diverge.
    """
    client = _HealthFakeNeo4jClient(
        counts=[{"label": "Person", "cnt": 2}],
        synthesis_failures=[],
        missing_vault_path=[],
        stale_entities=[],
    )

    first = run_health_check(client, staleness_days=90)
    second = run_health_check(client, staleness_days=90)

    assert first.ok is True
    assert second.ok is True
    assert first.total_entities == second.total_entities == 2
    assert first.synthesis_failures == second.synthesis_failures == []
    assert first.stale_entities == second.stale_entities == []
    # Both cycles exercised the same cypher patterns — staleness, vault, summary, count.
    assert len(client.cypher_calls) >= 8  # 4 patterns x 2 cycles


@pytest.mark.integration
def test_cycle_against_unavailable_neo4j_skips_queries_and_is_ok() -> None:
    """When ``client.available is False`` the cycle returns an empty report
    without firing any Cypher, leaving ``ok=True`` because there are no
    issues to surface.

    Sabotage: if the unavailable-branch fell through to the Cypher pass,
    ``cypher_calls`` would be non-empty.
    """

    class _DownClient(_HealthFakeNeo4jClient):
        available: bool = False

    client = _DownClient()
    report = run_health_check(client, staleness_days=90)

    assert report.neo4j_available is False
    assert report.total_entities == 0
    assert report.entities_by_type == {}
    assert report.ok is True
    assert client.cypher_calls == []
