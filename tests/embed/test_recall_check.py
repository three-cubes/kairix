"""
Tests for kairix.core.embed.recall_check

Covers:
- _get_recall_queries(): default, env override, adaptive from DB
- _build_adaptive_queries(): derive queries from indexed titles
- check_recall(): end-to-end with injected embed + search fakes
"""

from __future__ import annotations

import json
import sqlite3

import numpy as np
import pytest

from kairix.core.embed.recall_check import (
    _build_adaptive_queries,
    _get_recall_queries,
    check_recall,
)

# ---------------------------------------------------------------------------
# _get_recall_queries
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_get_recall_queries_returns_defaults() -> None:
    """Returns non-empty list of (id, query, gold_fragment) tuples by default."""
    queries = _get_recall_queries()
    assert len(queries) >= 1
    for row in queries:
        assert len(row) == 3
        qid, query, gold = row
        assert isinstance(qid, str)
        assert isinstance(query, str)
        assert isinstance(gold, str)


@pytest.mark.unit
def test_get_recall_queries_uses_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """RECALL_QUERIES env var overrides defaults when valid JSON."""
    custom = [["T1", "what is the test?", "test-fragment"]]
    monkeypatch.setenv("RECALL_QUERIES", json.dumps(custom))
    queries = _get_recall_queries()
    assert queries == [("T1", "what is the test?", "test-fragment")]


@pytest.mark.unit
def test_get_recall_queries_falls_back_on_bad_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Falls back to defaults when RECALL_QUERIES env var is invalid JSON."""
    monkeypatch.setenv("RECALL_QUERIES", "not-valid-json{{{")
    queries = _get_recall_queries()
    assert len(queries) >= 1  # returns defaults


# ---------------------------------------------------------------------------
# _build_adaptive_queries
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_adaptive_queries_from_db() -> None:
    """Builds queries from indexed document titles."""
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE documents (path TEXT, title TEXT, active INTEGER);
        INSERT INTO documents VALUES ('docs/architecture.md', 'architecture', 1);
        INSERT INTO documents VALUES ('docs/deploy-guide.md', 'deploy-guide', 1);
        INSERT INTO documents VALUES ('docs/testing.md', 'testing', 1);
    """)

    queries = _build_adaptive_queries(db)
    assert len(queries) == 3
    # Each query should be a tuple of (id, readable_title, path_stem)
    for qid, query, gold in queries:
        assert qid.startswith("A")
        assert isinstance(query, str)
        assert isinstance(gold, str)


@pytest.mark.unit
def test_build_adaptive_queries_empty_db() -> None:
    """Returns empty list when no documents indexed."""
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE documents (path TEXT, title TEXT, active INTEGER)")

    queries = _build_adaptive_queries(db)
    assert queries == []


@pytest.mark.unit
def test_build_adaptive_queries_no_table() -> None:
    """Returns empty list when documents table doesn't exist."""
    db = sqlite3.connect(":memory:")
    queries = _build_adaptive_queries(db)
    assert queries == []


@pytest.mark.unit
def test_adaptive_queries_used_when_db_available() -> None:
    """_get_recall_queries prefers adaptive queries over defaults when db is available."""
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE documents (path TEXT, title TEXT, active INTEGER);
        INSERT INTO documents VALUES ('docs/my-doc.md', 'my-doc', 1);
    """)

    queries = _get_recall_queries(db)
    assert len(queries) == 1
    assert queries[0][0] == "A01"


# ---------------------------------------------------------------------------
# check_recall — using DI (embed_fn, vsearch_fn, recall_queries)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_check_recall_skips_when_embed_returns_none() -> None:
    """check_recall() marks queries as skipped when embed_fn returns None."""
    db = sqlite3.connect(":memory:")

    result = check_recall(db=db, embed_fn=lambda _q: None)

    assert result["score"] == pytest.approx(0.0)
    assert result["passed"] == 0
    assert all(d.get("skipped") for d in result["detail"])


@pytest.mark.unit
def test_check_recall_returns_structure() -> None:
    """check_recall() always returns a dict with required keys."""
    db = sqlite3.connect(":memory:")

    result = check_recall(db=db, embed_fn=lambda _q: None)

    assert "score" in result
    assert "passed" in result
    assert "total" in result
    assert "detail" in result
    assert isinstance(result["detail"], list)


@pytest.mark.unit
def test_check_recall_counts_hit_when_gold_in_results() -> None:
    """check_recall() counts a hit when gold fragment appears in vsearch results."""
    db = sqlite3.connect(":memory:")

    fake_vec = np.array([0.1] * 1536, dtype=np.float32)
    fake_files = ["04-Agent-Knowledge/builder/patterns.md"]

    result = check_recall(
        db=db,
        embed_fn=lambda _q: fake_vec,
        vsearch_fn=lambda _vec, _k: fake_files,
        recall_queries=[("R1", "engineering patterns", "builder/patterns")],
    )

    assert result["passed"] == 1
    assert result["score"] == pytest.approx(1.0)
    assert result["detail"][0]["hit"] is True


@pytest.mark.unit
def test_embed_query_uses_injected_embed_provider() -> None:
    """Regression: _embed_query routes through EmbedProvider, not raw HTTP.

    Closes the last #43 wire-up. Constructed with a FakeEmbedProvider from
    tests/fakes.py — no monkeypatch, no module-level import substitution.
    The fake captures call args so we can assert the provider received the
    right query, model, and dims.
    """
    from kairix.core.embed.recall_check import _embed_query
    from tests.fakes import FakeEmbedProvider

    fake = FakeEmbedProvider(vector=[0.0, 0.6, 0.8])
    arr = _embed_query("kairix MCP path", provider=fake, model="text-embedding-3-large")

    assert arr is not None
    assert len(fake.calls) == 1
    assert fake.calls[0]["texts"] == ["kairix MCP path"]
    assert fake.calls[0]["model"] == "text-embedding-3-large"
    # Returned vector is unit-normalised (within float32 tolerance)
    assert abs(float(np.linalg.norm(arr)) - 1.0) < 1e-5


@pytest.mark.unit
def test_embed_query_returns_none_on_provider_failure() -> None:
    """Provider exception → returns None (logged warning, no raise)."""
    from kairix.core.embed.recall_check import _embed_query

    class _ExplodingProvider:
        def embed_batch(self, texts: list[str], *, model: str, dims: int) -> list[list[float]]:
            raise RuntimeError("provider failed")

    arr = _embed_query("anything", provider=_ExplodingProvider(), model="m")
    assert arr is None
