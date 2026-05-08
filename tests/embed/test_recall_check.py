"""Unit tests for kairix.core.embed.recall_check.

All injection happens via ``RecallChecker(embed_provider=..., vector_searcher=...)``
constructor args using ``FakeEmbedProvider`` / ``FakeVectorSearcher`` from
tests/fakes.py. No monkeypatch, no @patch, no setattr, no ``*_fn=`` kwargs.
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
    _build_adaptive_queries,
    _embed_query,
    _get_recall_queries,
    check_recall,
    load_previous_score,
    run_recall_gate,
    save_recall_result,
)
from tests.fakes import FakeEmbedProvider, FakeVectorSearcher

# ---------------------------------------------------------------------------
# _get_recall_queries
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_recall_queries_returns_default_list_with_no_db() -> None:
    """Without a DB, returns the static DEFAULT_RECALL_QUERIES verbatim."""
    queries = _get_recall_queries(None)
    assert queries == list(DEFAULT_RECALL_QUERIES)


@pytest.mark.unit
def test_get_recall_queries_returns_default_list_when_db_has_no_documents() -> None:
    """An empty documents table falls through to the defaults."""
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE documents (path TEXT, title TEXT, active INTEGER)")
    queries = _get_recall_queries(db)
    assert queries == list(DEFAULT_RECALL_QUERIES)


@pytest.mark.unit
def test_get_recall_queries_uses_adaptive_when_db_has_documents() -> None:
    """When the documents table has indexed titles, adaptive queries take precedence."""
    db = sqlite3.connect(":memory:")
    db.executescript(
        """
        CREATE TABLE documents (path TEXT, title TEXT, active INTEGER);
        INSERT INTO documents VALUES ('docs/my-doc.md', 'my-doc', 1);
        """
    )
    queries = _get_recall_queries(db)
    assert len(queries) == 1
    assert queries[0][0] == "A01"  # adaptive id prefix
    assert queries[0][1] == "my doc"  # title with dashes replaced
    assert queries[0][2] == "my-doc"  # path stem


# ---------------------------------------------------------------------------
# _build_adaptive_queries
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_adaptive_queries_from_three_indexed_documents() -> None:
    """Three indexed titles produce three adaptive queries."""
    db = sqlite3.connect(":memory:")
    db.executescript(
        """
        CREATE TABLE documents (path TEXT, title TEXT, active INTEGER);
        INSERT INTO documents VALUES ('docs/architecture.md', 'architecture', 1);
        INSERT INTO documents VALUES ('docs/deploy-guide.md', 'deploy-guide', 1);
        INSERT INTO documents VALUES ('docs/testing.md', 'testing', 1);
        """
    )
    queries = _build_adaptive_queries(db)
    assert len(queries) == 3
    qids = {q[0] for q in queries}
    assert qids == {"A01", "A02", "A03"}
    paths_via_gold = {q[2] for q in queries}
    assert paths_via_gold == {"architecture", "deploy-guide", "testing"}


@pytest.mark.unit
def test_build_adaptive_queries_excludes_inactive_and_titleless_documents() -> None:
    """Inactive rows and rows with NULL/empty title are excluded from the sample."""
    db = sqlite3.connect(":memory:")
    db.executescript(
        """
        CREATE TABLE documents (path TEXT, title TEXT, active INTEGER);
        INSERT INTO documents VALUES ('docs/keep.md', 'keep', 1);
        INSERT INTO documents VALUES ('docs/inactive.md', 'inactive', 0);
        INSERT INTO documents VALUES ('docs/empty.md', '', 1);
        INSERT INTO documents VALUES ('docs/null.md', NULL, 1);
        """
    )
    queries = _build_adaptive_queries(db)
    gold_fragments = {q[2] for q in queries}
    assert gold_fragments == {"keep"}


@pytest.mark.unit
def test_build_adaptive_queries_returns_empty_when_documents_table_missing() -> None:
    """Missing documents table → empty list, no exception."""
    db = sqlite3.connect(":memory:")
    queries = _build_adaptive_queries(db)
    assert queries == []


# ---------------------------------------------------------------------------
# _embed_query — DI via FakeEmbedProvider
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_embed_query_normalises_provider_output_to_unit_vector() -> None:
    """Embedded vector must be unit-normalised (||v|| == 1) for cosine similarity."""
    fake = FakeEmbedProvider(vector=[0.0, 3.0, 4.0])  # raw norm = 5.0
    arr = _embed_query("anything", provider=fake, model="text-embedding-3-large")
    assert arr is not None
    assert abs(float(np.linalg.norm(arr)) - 1.0) < 1e-5
    # Normalised values: 0/5, 3/5, 4/5
    assert arr[0] == pytest.approx(0.0, abs=1e-5)
    assert arr[1] == pytest.approx(0.6, abs=1e-5)
    assert arr[2] == pytest.approx(0.8, abs=1e-5)


@pytest.mark.unit
def test_embed_query_passes_model_and_dims_through_to_provider() -> None:
    """The provider receives the configured model name and the EMBED_DIMS constant."""
    from kairix.core.embed.schema import EMBED_VECTOR_DIMS

    fake = FakeEmbedProvider(vector=[0.0, 1.0])
    _embed_query("query text", provider=fake, model="text-embedding-3-small")
    assert len(fake.calls) == 1
    assert fake.calls[0]["texts"] == ["query text"]
    assert fake.calls[0]["model"] == "text-embedding-3-small"
    assert fake.calls[0]["dims"] == EMBED_VECTOR_DIMS


@pytest.mark.unit
def test_embed_query_returns_none_when_provider_raises() -> None:
    """Provider exception → returns None (no propagation)."""

    class _ExplodingProvider:
        def embed_batch(self, texts: list[str], *, model: str, dims: int) -> list[list[float]]:
            raise RuntimeError("provider failed")

    arr = _embed_query("anything", provider=_ExplodingProvider(), model="m")
    assert arr is None


@pytest.mark.unit
def test_embed_query_returns_none_when_provider_returns_empty_list() -> None:
    """An empty embedding list → None."""

    class _EmptyProvider:
        def embed_batch(self, texts: list[str], *, model: str, dims: int) -> list[list[float]]:
            return []

    arr = _embed_query("any", provider=_EmptyProvider(), model="m")
    assert arr is None


# ---------------------------------------------------------------------------
# RecallChecker.check — class-method tests via FakeEmbedProvider + FakeVectorSearcher
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_recall_checker_skips_query_when_embed_returns_none() -> None:
    """When the provider can't embed, the query is recorded as skipped (not failed)."""

    class _AlwaysEmptyProvider:
        def embed_batch(self, texts: list[str], *, model: str, dims: int) -> list[list[float]]:
            return []  # returns None inside _embed_query

    checker = RecallChecker(embed_provider=_AlwaysEmptyProvider(), vector_searcher=FakeVectorSearcher())
    queries = [("R1", "engineering", "engineering")]
    result = checker.check(recall_queries=queries)

    assert result["score"] == 0.0
    assert result["passed"] == 0
    assert result["total"] == 0  # skipped queries are excluded from total
    assert len(result["detail"]) == 1
    assert result["detail"][0]["skipped"] is True
    assert result["detail"][0]["hit"] is False


@pytest.mark.unit
def test_recall_checker_counts_a_hit_when_gold_fragment_appears_in_search_results() -> None:
    """A returned path containing the gold fragment counts as a hit; score is 1.0."""
    fake_vector = FakeEmbedProvider(vector=[1.0, 0.0, 0.0])
    fake_searcher = FakeVectorSearcher(paths=["04-Agent-Knowledge/builder/patterns.md"])

    checker = RecallChecker(embed_provider=fake_vector, vector_searcher=fake_searcher)
    queries = [("R1", "engineering patterns", "builder/patterns")]
    result = checker.check(recall_queries=queries)

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
    db = sqlite3.connect(":memory:")
    db.executescript(
        """
        CREATE TABLE documents (path TEXT, title TEXT, active INTEGER);
        INSERT INTO documents VALUES ('docs/architecture.md', 'architecture', 1);
        """
    )
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
    # Specifically: only one new entry was added; if the cap had failed we'd see 91.


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
# Production defaults — exercise the lazy-construction fallbacks
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_usearch_vector_searcher_returns_empty_when_index_unavailable() -> None:
    """The production VectorSearcher returns [] when usearch is not initialised.

    In the test environment ``get_vector_index()`` either returns None or raises;
    either way the production code must surface this as ``[]`` (not a raise) so
    the recall gate cleanly degrades to "all queries skipped".
    """
    from kairix.core.embed.recall_check import _UsearchVectorSearcher

    searcher = _UsearchVectorSearcher()
    vec = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    result = searcher.search_vectors(vec, limit=5)
    assert result == []


@pytest.mark.unit
def test_recall_checker_lazy_constructs_default_vector_searcher_when_none_provided() -> None:
    """When ``vector_searcher=None``, ``_search`` lazily builds a ``_UsearchVectorSearcher``.

    The lazy instance is cached on the checker so subsequent calls reuse it.
    In the test environment the lazy default returns [] (no usearch index),
    so every query is recorded as a miss with ``returned=[]``.
    """
    checker = RecallChecker(embed_provider=FakeEmbedProvider(), vector_searcher=None)
    assert checker._vector_searcher is None
    result = checker.check(recall_queries=[("R1", "any query", "fragment")])

    # The default searcher fell through to []: query is graded but didn't hit.
    assert result["total"] == 1
    assert result["passed"] == 0
    assert result["detail"][0]["returned"] == []
    assert result["detail"][0]["hit"] is False
    # Lazy default was constructed and cached.
    assert checker._vector_searcher is not None
    cached = checker._vector_searcher
    # A second call reuses the same instance (cache check).
    checker.check(recall_queries=[("R2", "another", "frag")])
    assert checker._vector_searcher is cached


@pytest.fixture
def _kairix_db_at_env_path(tmp_path: Path) -> Path:
    """Build a documents table at a temp path and point KAIRIX_DB_PATH at it.

    Direct ``os.environ`` manipulation rather than ``monkeypatch.setenv`` —
    KAIRIX_DB_PATH is operator-facing configuration the production CLI sets,
    not a code-level test seam.
    """
    import os

    db_path = tmp_path / "kairix.sqlite"
    db = sqlite3.connect(str(db_path))
    db.executescript(
        """
        CREATE TABLE documents (id INTEGER PRIMARY KEY, path TEXT, title TEXT, active INTEGER, hash TEXT);
        INSERT INTO documents (path, title, active, hash) VALUES ('docs/architecture.md', 'architecture', 1, 'h0');
        """
    )
    db.commit()
    db.close()

    prev = os.environ.get("KAIRIX_DB_PATH")
    os.environ["KAIRIX_DB_PATH"] = str(db_path)
    yield db_path
    if prev is None:
        os.environ.pop("KAIRIX_DB_PATH", None)
    else:
        os.environ["KAIRIX_DB_PATH"] = prev


@pytest.mark.unit
def test_recall_checker_check_opens_db_when_db_arg_is_none(_kairix_db_at_env_path: Path) -> None:
    """When ``db=None``, ``check`` opens the kairix DB at ``KAIRIX_DB_PATH`` and uses adaptive queries."""
    fake_searcher = FakeVectorSearcher(paths=["docs/architecture.md"])
    checker = RecallChecker(embed_provider=FakeEmbedProvider(), vector_searcher=fake_searcher)
    result = checker.check()

    assert result["total"] == 1
    assert result["detail"][0]["query"] == "architecture"
    assert result["detail"][0]["hit"] is True


@pytest.mark.unit
def test_recall_checker_check_falls_through_to_defaults_when_db_path_missing(tmp_path: Path) -> None:
    """When ``db=None`` and ``KAIRIX_DB_PATH`` points at a non-existent file, falls back to default queries."""
    import os

    # Point at a path that does not exist; production raises FileNotFoundError which
    # the check() method catches, leaving db=None so the loop uses DEFAULT_RECALL_QUERIES.
    missing = tmp_path / "this-does-not-exist.sqlite"
    prev = os.environ.get("KAIRIX_DB_PATH")
    os.environ["KAIRIX_DB_PATH"] = str(missing)
    try:
        # FakeVectorSearcher returns [] — every default query is recorded as a miss.
        checker = RecallChecker(embed_provider=FakeEmbedProvider(), vector_searcher=FakeVectorSearcher())
        result = checker.check()
        # The default static queries were used (5 of them).
        assert result["total"] == len(DEFAULT_RECALL_QUERIES)
    finally:
        if prev is None:
            os.environ.pop("KAIRIX_DB_PATH", None)
        else:
            os.environ["KAIRIX_DB_PATH"] = prev


@pytest.mark.unit
def test_run_recall_gate_constructs_default_checker_when_none_supplied(tmp_path: Path) -> None:
    """When ``checker=None``, ``run_recall_gate`` builds a default ``RecallChecker``.

    The default checker has no embed_provider, so every query is skipped (returns
    None from _embed_query). The gate still runs end-to-end and writes a log.
    """
    import os

    # No KAIRIX_DB_PATH → the lazy DB open falls through to None, default queries used,
    # all of them skipped because no embed credentials.
    prev = os.environ.get("KAIRIX_DB_PATH")
    os.environ["KAIRIX_DB_PATH"] = str(tmp_path / "no-such-db.sqlite")
    try:
        passed, result = run_recall_gate(log_path=tmp_path / "log.json")
        assert passed is True  # no previous score, no degradation comparison
        # All default queries skipped because no embed credentials → total=0, score=0.0
        assert result["total"] == 0
        assert all(d["skipped"] for d in result["detail"])
    finally:
        if prev is None:
            os.environ.pop("KAIRIX_DB_PATH", None)
        else:
            os.environ["KAIRIX_DB_PATH"] = prev
