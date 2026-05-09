"""Unit tests for kairix.core.embed.recall_check.

All injection happens via ``RecallChecker(embed_provider=..., vector_searcher=...)``
constructor args using ``FakeEmbedProvider`` / ``FakeVectorSearcher`` from
tests/fakes.py. No private helpers are imported — every branch is driven
through ``RecallChecker.check`` / ``run_recall_gate`` and observed via the
returned result dict (``detail``, ``passed``, ``total``, ``score``).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from kairix.core.embed.recall_check import (
    DEFAULT_RECALL_QUERIES,
    RecallChecker,
    check_recall,
    load_previous_score,
    run_recall_gate,
    save_recall_result,
)
from tests.fakes import FakeEmbedProvider, FakeVectorSearcher

# ---------------------------------------------------------------------------
# Recall-query selection — driven through RecallChecker.check(db=...)
#
# detail[i]["id"] / detail[i]["query"] reveal which queries the checker
# selected: defaults (R01..R05) when the DB has no usable docs, adaptive
# (A01..) when it does.
# ---------------------------------------------------------------------------


def _make_documents_db(rows: list[tuple[str, str | None, int]]) -> sqlite3.Connection:
    """Build an in-memory ``documents`` table from ``(path, title, active)`` rows."""
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE documents (path TEXT, title TEXT, active INTEGER)")
    db.executemany("INSERT INTO documents (path, title, active) VALUES (?, ?, ?)", rows)
    db.commit()
    return db


@pytest.mark.unit
def test_check_uses_default_queries_when_db_has_no_documents() -> None:
    """An empty documents table falls through to DEFAULT_RECALL_QUERIES (R01..R05)."""
    db = _make_documents_db([])
    checker = RecallChecker(embed_provider=FakeEmbedProvider(), vector_searcher=FakeVectorSearcher())
    result = checker.check(db=db)

    assert len(result["detail"]) == len(DEFAULT_RECALL_QUERIES)
    ids = [d["id"] for d in result["detail"]]
    assert ids == [q[0] for q in DEFAULT_RECALL_QUERIES]


@pytest.mark.unit
def test_check_uses_three_adaptive_queries_for_three_indexed_documents() -> None:
    """Three indexed titles produce three adaptive queries (A01..A03) ordered by row."""
    db = _make_documents_db(
        [
            ("docs/architecture.md", "architecture", 1),
            ("docs/deploy-guide.md", "deploy-guide", 1),
            ("docs/testing.md", "testing", 1),
        ]
    )
    checker = RecallChecker(embed_provider=FakeEmbedProvider(), vector_searcher=FakeVectorSearcher())
    result = checker.check(db=db)

    assert len(result["detail"]) == 3
    assert {d["id"] for d in result["detail"]} == {"A01", "A02", "A03"}
    # Each adaptive entry's query is the title with dashes/underscores replaced;
    # the gold_fragment is the path stem.
    gold_fragments = {d["gold_fragment"] for d in result["detail"]}
    assert gold_fragments == {"architecture", "deploy-guide", "testing"}


@pytest.mark.unit
def test_check_excludes_inactive_and_titleless_documents_from_adaptive_queries() -> None:
    """Inactive / NULL-title / empty-title rows do not contribute adaptive queries."""
    db = _make_documents_db(
        [
            ("docs/keep.md", "keep", 1),
            ("docs/inactive.md", "inactive", 0),
            ("docs/empty.md", "", 1),
            ("docs/null.md", None, 1),
        ]
    )
    checker = RecallChecker(embed_provider=FakeEmbedProvider(), vector_searcher=FakeVectorSearcher())
    result = checker.check(db=db)

    # Only the one valid row makes it into adaptive queries.
    assert len(result["detail"]) == 1
    assert result["detail"][0]["gold_fragment"] == "keep"


@pytest.mark.unit
def test_check_falls_back_to_defaults_when_documents_table_is_missing() -> None:
    """A DB with no ``documents`` table → adaptive returns [] → defaults are used."""
    db = sqlite3.connect(":memory:")
    checker = RecallChecker(embed_provider=FakeEmbedProvider(), vector_searcher=FakeVectorSearcher())
    result = checker.check(db=db)

    assert len(result["detail"]) == len(DEFAULT_RECALL_QUERIES)
    assert [d["id"] for d in result["detail"]] == [q[0] for q in DEFAULT_RECALL_QUERIES]


# ---------------------------------------------------------------------------
# Embedding plumbing — observed via FakeEmbedProvider.calls and
# FakeVectorSearcher.calls after RecallChecker.check.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_check_passes_query_text_model_and_canonical_dims_to_provider() -> None:
    """The embed provider receives the query text, the chosen model name, and EMBED_VECTOR_DIMS."""
    from kairix.core.embed.schema import EMBED_VECTOR_DIMS

    fake_provider = FakeEmbedProvider(vector=[0.0, 1.0])
    checker = RecallChecker(embed_provider=fake_provider, vector_searcher=FakeVectorSearcher())
    checker.check(recall_queries=[("R1", "specific query text", "x")])

    assert len(fake_provider.calls) == 1
    call = fake_provider.calls[0]
    assert call["texts"] == ["specific query text"]
    assert call["model"] == "text-embedding-3-large"
    assert call["dims"] == EMBED_VECTOR_DIMS


@pytest.mark.unit
def test_check_normalises_embedded_vector_to_unit_length_before_searching() -> None:
    """The vector handed to the searcher must be unit-normalised — cosine similarity requires it."""
    fake_provider = FakeEmbedProvider(vector=[0.0, 3.0, 4.0])  # raw L2 norm = 5.0
    fake_searcher = FakeVectorSearcher(paths=["docs/x.md"])
    checker = RecallChecker(embed_provider=fake_provider, vector_searcher=fake_searcher)
    checker.check(recall_queries=[("R1", "any", "x")])

    assert len(fake_searcher.calls) == 1
    fed_vector = fake_searcher.calls[0]["vector"]
    norm = float(np.linalg.norm(fed_vector))
    assert abs(norm - 1.0) < 1e-5
    # Normalised values: 0/5, 3/5, 4/5
    assert fed_vector[0] == pytest.approx(0.0, abs=1e-5)
    assert fed_vector[1] == pytest.approx(0.6, abs=1e-5)
    assert fed_vector[2] == pytest.approx(0.8, abs=1e-5)


@pytest.mark.unit
def test_check_skips_query_when_provider_raises() -> None:
    """Provider exception → query is recorded as skipped, not propagated."""

    class _ExplodingProvider:
        def embed_batch(self, texts: list[str], *, model: str, dims: int) -> list[list[float]]:
            raise RuntimeError("provider failed")

    checker = RecallChecker(embed_provider=_ExplodingProvider(), vector_searcher=FakeVectorSearcher())
    result = checker.check(recall_queries=[("R1", "any", "x")])

    assert result["total"] == 0
    assert result["detail"][0]["skipped"] is True


# ---------------------------------------------------------------------------
# RecallChecker.check — semantics with FakeEmbedProvider + FakeVectorSearcher.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_recall_checker_skips_query_when_provider_returns_empty_embedding_list() -> None:
    """An empty-list provider response is treated as a skip (not a failed query)."""

    class _AlwaysEmptyProvider:
        def embed_batch(self, texts: list[str], *, model: str, dims: int) -> list[list[float]]:
            return []  # surfaces as None inside the embedding step

    checker = RecallChecker(embed_provider=_AlwaysEmptyProvider(), vector_searcher=FakeVectorSearcher())
    result = checker.check(recall_queries=[("R1", "engineering", "engineering")])

    assert result["score"] == 0.0
    assert result["passed"] == 0
    assert result["total"] == 0  # skipped queries are excluded from the denominator
    assert len(result["detail"]) == 1
    assert result["detail"][0]["skipped"] is True
    assert result["detail"][0]["hit"] is False


@pytest.mark.unit
def test_recall_checker_counts_a_hit_when_gold_fragment_appears_in_search_results() -> None:
    """A returned path containing the gold fragment counts as a hit; score is 1.0."""
    fake_provider = FakeEmbedProvider(vector=[1.0, 0.0, 0.0])
    fake_searcher = FakeVectorSearcher(paths=["04-Agent-Knowledge/builder/patterns.md"])

    checker = RecallChecker(embed_provider=fake_provider, vector_searcher=fake_searcher)
    result = checker.check(recall_queries=[("R1", "engineering patterns", "builder/patterns")])

    assert result["passed"] == 1
    assert result["total"] == 1
    assert result["score"] == pytest.approx(1.0)
    assert result["detail"][0]["hit"] is True
    assert result["detail"][0]["returned"] == ["04-Agent-Knowledge/builder/patterns.md"]


@pytest.mark.unit
def test_recall_checker_misses_when_gold_fragment_not_in_any_returned_path() -> None:
    """The gold fragment is absent from the returned paths → hit=False, score=0.0."""
    fake_searcher = FakeVectorSearcher(paths=["docs/unrelated.md", "docs/other.md"])
    checker = RecallChecker(embed_provider=FakeEmbedProvider(), vector_searcher=fake_searcher)
    result = checker.check(recall_queries=[("R1", "anything", "missing-fragment")])

    assert result["passed"] == 0
    assert result["total"] == 1
    assert result["score"] == 0.0
    assert result["detail"][0]["hit"] is False
    assert result["detail"][0]["returned"] == ["docs/unrelated.md", "docs/other.md"]


@pytest.mark.unit
def test_recall_checker_score_is_passed_over_total_excluding_skips() -> None:
    """Score arithmetic: 1 hit / 2 checked = 0.5; a third skipped query doesn't lower the denominator."""

    class _ConditionalProvider:
        def __init__(self) -> None:
            self.call_count = 0

        def embed_batch(self, texts: list[str], *, model: str, dims: int) -> list[list[float]]:
            self.call_count += 1
            if self.call_count == 3:
                return []  # skip the 3rd query
            return [[1.0, 0.0]]

    fake_searcher = FakeVectorSearcher(paths=["docs/match-here.md"])
    checker = RecallChecker(embed_provider=_ConditionalProvider(), vector_searcher=fake_searcher)
    queries = [
        ("R1", "q1", "match"),  # hit
        ("R2", "q2", "no-match-here"),  # miss
        ("R3", "q3", "anything"),  # skipped
    ]
    result = checker.check(recall_queries=queries)

    assert result["passed"] == 1
    assert result["total"] == 2  # only non-skipped are counted
    assert result["score"] == pytest.approx(0.5)


@pytest.mark.unit
def test_recall_checker_uses_adaptive_queries_when_db_has_documents() -> None:
    """With db= but no recall_queries=, the checker derives queries adaptively."""
    db = _make_documents_db([("docs/architecture.md", "architecture", 1)])
    fake_searcher = FakeVectorSearcher(paths=["docs/architecture.md"])
    checker = RecallChecker(embed_provider=FakeEmbedProvider(), vector_searcher=fake_searcher)
    result = checker.check(db=db)

    assert result["total"] == 1
    assert result["detail"][0]["id"] == "A01"
    assert result["detail"][0]["query"] == "architecture"
    assert result["detail"][0]["hit"] is True


@pytest.mark.unit
def test_check_recall_shim_returns_dict_with_expected_keys() -> None:
    """The free-function shim returns the same shape RecallChecker.check produces."""
    db = sqlite3.connect(":memory:")
    result = check_recall(db=db, recall_queries=[("R1", "q", "g")])
    # No vector_searcher / embed_provider injected → defaults try real Azure/usearch,
    # which fail in test env → the query is skipped.
    assert {"score", "passed", "total", "timestamp", "detail"} <= set(result.keys())
    assert result["detail"][0]["skipped"] is True


# ---------------------------------------------------------------------------
# load_previous_score / save_recall_result — log-file persistence
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_previous_score_returns_none_when_log_does_not_exist(tmp_path: Path) -> None:
    """No log file → no previous score."""
    assert load_previous_score(tmp_path / "nope.json") is None


@pytest.mark.unit
def test_load_previous_score_returns_score_from_last_entry(tmp_path: Path) -> None:
    """The most recent run's score is returned."""
    log = tmp_path / "log.json"
    log.write_text(json.dumps([{"score": 0.7}, {"score": 0.85}]))
    assert load_previous_score(log) == pytest.approx(0.85)


@pytest.mark.unit
def test_load_previous_score_returns_none_on_corrupt_json(tmp_path: Path) -> None:
    """Garbled JSON → None, no propagation."""
    log = tmp_path / "log.json"
    log.write_text("{not valid json")
    assert load_previous_score(log) is None


@pytest.mark.unit
def test_load_previous_score_returns_none_when_log_is_empty_list(tmp_path: Path) -> None:
    """An empty list of runs → None."""
    log = tmp_path / "log.json"
    log.write_text("[]")
    assert load_previous_score(log) is None


@pytest.mark.unit
def test_save_recall_result_appends_to_existing_log(tmp_path: Path) -> None:
    """Saving a new result appends; the previous run is preserved."""
    log = tmp_path / "log.json"
    log.write_text(json.dumps([{"score": 0.5}]))
    save_recall_result({"score": 0.7}, log)

    runs = json.loads(log.read_text())
    assert len(runs) == 2
    assert runs[0]["score"] == 0.5
    assert runs[1]["score"] == 0.7


@pytest.mark.unit
def test_save_recall_result_caps_log_at_90_entries(tmp_path: Path) -> None:
    """The log keeps only the most recent 90 entries to bound disk use."""
    log = tmp_path / "log.json"
    log.write_text(json.dumps([{"score": 0.0}] * 90))
    save_recall_result({"score": 0.99}, log)

    runs = json.loads(log.read_text())
    assert len(runs) == 90
    assert runs[-1]["score"] == 0.99
    # The oldest entry was dropped.
    assert runs[0]["score"] == 0.0


@pytest.mark.unit
def test_save_recall_result_recovers_from_corrupt_log(tmp_path: Path) -> None:
    """A corrupt existing log is replaced rather than crashing the save path."""
    log = tmp_path / "log.json"
    log.write_text("{garbled")
    save_recall_result({"score": 0.5}, log)

    runs = json.loads(log.read_text())
    assert runs == [{"score": 0.5}]


@pytest.mark.unit
def test_save_recall_result_creates_parent_directory(tmp_path: Path) -> None:
    """Save must create the cache directory if it doesn't exist."""
    log = tmp_path / "subdir" / "nested" / "log.json"
    save_recall_result({"score": 0.42}, log)
    assert log.exists()
    assert json.loads(log.read_text()) == [{"score": 0.42}]


# ---------------------------------------------------------------------------
# run_recall_gate — end-to-end with injection seams
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_run_recall_gate_passes_on_first_run_with_no_previous_log(tmp_path: Path) -> None:
    """No previous score → no degradation comparison; gate returns passed=True."""
    log = tmp_path / "log.json"
    fake_searcher = FakeVectorSearcher(paths=["docs/architecture.md"])

    class _StaticChecker(RecallChecker):
        def check(
            self,
            *,
            db: sqlite3.Connection | None = None,
            recall_queries: list[tuple[str, str, str]] | None = None,
        ) -> dict[str, object]:
            return {"score": 0.85, "passed": 4, "total": 5, "timestamp": 0, "detail": []}

    passed, result = run_recall_gate(
        checker=_StaticChecker(embed_provider=FakeEmbedProvider(), vector_searcher=fake_searcher),
        log_path=log,
    )
    assert passed is True
    assert result["score"] == pytest.approx(0.85)
    # The log was written.
    runs = json.loads(log.read_text())
    assert runs[-1]["score"] == pytest.approx(0.85)


@pytest.mark.unit
def test_run_recall_gate_fails_and_invokes_alert_when_score_drops_below_threshold(tmp_path: Path) -> None:
    """A 0.30 drop (>10%) triggers the alert callback and returns passed=False."""
    log = tmp_path / "log.json"
    log.write_text(json.dumps([{"score": 0.95}]))  # previous run

    captured_alerts: list[str] = []

    class _StaticChecker(RecallChecker):
        def check(self, **kwargs: object) -> dict[str, object]:
            return {"score": 0.65, "passed": 3, "total": 5, "timestamp": 0, "detail": []}

    passed, result = run_recall_gate(
        alert_callback=captured_alerts.append,
        checker=_StaticChecker(),
        log_path=log,
    )

    assert passed is False
    assert result["score"] == pytest.approx(0.65)
    assert len(captured_alerts) == 1
    assert "65%" in captured_alerts[0]
    assert "95%" in captured_alerts[0]
    assert "-30%" in captured_alerts[0]


@pytest.mark.unit
def test_run_recall_gate_passes_when_score_drops_within_threshold(tmp_path: Path) -> None:
    """A 5% drop (<10% threshold) does not trigger the alert; gate passes."""
    log = tmp_path / "log.json"
    log.write_text(json.dumps([{"score": 0.90}]))

    captured_alerts: list[str] = []

    class _StaticChecker(RecallChecker):
        def check(self, **kwargs: object) -> dict[str, object]:
            return {"score": 0.85, "passed": 4, "total": 5, "timestamp": 0, "detail": []}

    passed, _ = run_recall_gate(
        alert_callback=captured_alerts.append,
        checker=_StaticChecker(),
        log_path=log,
    )
    assert passed is True
    assert captured_alerts == []


@pytest.mark.unit
def test_run_recall_gate_passes_when_score_improves(tmp_path: Path) -> None:
    """A score increase always passes the gate; alert callback never fires."""
    log = tmp_path / "log.json"
    log.write_text(json.dumps([{"score": 0.70}]))

    captured: list[str] = []

    class _StaticChecker(RecallChecker):
        def check(self, **kwargs: object) -> dict[str, object]:
            return {"score": 0.92, "passed": 5, "total": 5, "timestamp": 0, "detail": []}

    passed, _ = run_recall_gate(
        alert_callback=captured.append,
        checker=_StaticChecker(),
        log_path=log,
    )
    assert passed is True
    assert captured == []


@pytest.mark.unit
def test_run_recall_gate_does_not_invoke_alert_when_callback_is_none(tmp_path: Path) -> None:
    """A degraded score with no callback still returns passed=False — no exception either."""
    log = tmp_path / "log.json"
    log.write_text(json.dumps([{"score": 0.95}]))

    class _StaticChecker(RecallChecker):
        def check(self, **kwargs: object) -> dict[str, object]:
            return {"score": 0.50, "passed": 2, "total": 5, "timestamp": 0, "detail": []}

    passed, _ = run_recall_gate(
        alert_callback=None,
        checker=_StaticChecker(),
        log_path=log,
    )
    assert passed is False


# ---------------------------------------------------------------------------
# Default-DB wiring — KAIRIX_DB_PATH driven via direct os.environ.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_recall_checker_falls_through_to_defaults_when_db_has_no_recall_queries_table(
    tmp_path: Path,
) -> None:
    """A db connection with no recall_queries table falls through to DEFAULT_RECALL_QUERIES.

    Drives the default-queries fallback path explicitly via ``db=`` rather than
    relying on KAIRIX_DB_PATH env-resolution; the env-fallback chain itself
    (get_db_path → open_db) is covered separately by tests/test_paths.py.
    """
    db_path = tmp_path / "no-recall-table.sqlite"
    db = sqlite3.connect(str(db_path))
    # Just a documents table — _get_recall_queries probes for a recall_queries
    # table; absent → returns DEFAULT_RECALL_QUERIES.
    db.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, path TEXT)")
    db.commit()

    checker = RecallChecker(embed_provider=FakeEmbedProvider(), vector_searcher=FakeVectorSearcher())
    try:
        result = checker.check(db=db)
    finally:
        db.close()

    assert result["total"] == len(DEFAULT_RECALL_QUERIES), (
        f"expected {len(DEFAULT_RECALL_QUERIES)} default queries, got {result['total']}"
    )


@pytest.mark.unit
def test_run_recall_gate_with_explicit_checker_writes_log(tmp_path: Path) -> None:
    """run_recall_gate writes a log and returns (passed, result) given an explicit checker.

    The 'checker=None → default checker' branch is environment-coupled and is
    covered by integration tests of the full embed CLI (which legitimately
    drives env-resolved defaults end-to-end).
    """
    # FakeEmbedProvider(empty=True) → embed_batch returns [] → recall_check skips every query.
    checker = RecallChecker(embed_provider=FakeEmbedProvider(empty=True), vector_searcher=FakeVectorSearcher())
    passed, result = run_recall_gate(checker=checker, log_path=tmp_path / "log.json")

    assert passed is True  # no previous score → no degradation comparison
    assert result["total"] == 0
    assert all(d["skipped"] for d in result["detail"])
    assert (tmp_path / "log.json").exists(), "run_recall_gate should have persisted the log"
