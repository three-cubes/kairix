"""Unit tests for the persistent recall canary cache.

The canary cache (``~/.cache/kairix/recall-canaries.json`` in production) is
the fix for v2026.5.10's worker restart-loop. Before the cache, every
recall check sampled five random documents from the corpus and compared
their hit rate against the previous run's score — but the previous run
sampled five DIFFERENT documents. The "delta -60%" alert was comparing
unrelated query sets.

These tests pin the contract by exercising the **public** API
(``RecallChecker.check`` + ``load_canary_cache`` / ``save_canary_cache``)
and observing the cache file as a side effect. We deliberately do not
reach for ``get_recall_queries`` directly — driving the canary-cache
behaviour through ``RecallChecker.check`` exercises the same surface
production callers hit.

Pinned behaviour:

  - First call with a populated DB writes a cache file with the sampled
    queries; the queries are persisted in a stable order with
    schema-versioned JSON.
  - Subsequent calls load the cached queries; the DB is not consulted
    again until the cache is rebuilt.
  - ``rebuild_canaries=True`` discards the cache and re-samples.
  - A schema-mismatched cache file is rejected and rebuilt.
  - A corrupt cache file is rejected and rebuilt.
  - Empty corpus + missing cache → defaults are used and no cache file
    is written (we do not cache the static defaults).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from kairix.core.embed.recall_check import (
    CANARY_CACHE_VERSION,
    DEFAULT_RECALL_QUERIES,
    RecallChecker,
    load_canary_cache,
    save_canary_cache,
)
from tests.fakes import FakeEmbedProvider, FakeVectorSearcher


def _make_documents_db(rows: list[tuple[str, str | None, int]]) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE documents (path TEXT, title TEXT, active INTEGER)")
    db.executemany("INSERT INTO documents (path, title, active) VALUES (?, ?, ?)", rows)
    db.commit()
    return db


def _checker() -> RecallChecker:
    """Construct a checker with deterministic fakes — production
    embed/vector behaviour is exercised in integration tests."""
    return RecallChecker(embed_provider=FakeEmbedProvider(), vector_searcher=FakeVectorSearcher())


@pytest.mark.unit
def test_first_call_with_populated_db_persists_queries_to_cache(tmp_path: Path) -> None:
    """First call samples from corpus and writes the cache file."""
    cache = tmp_path / "canaries.json"
    db = _make_documents_db([("docs/foo.md", "foo", 1), ("docs/bar.md", "bar", 1)])

    result = _checker().check(db=db, canary_cache_path=cache)

    assert result["total"] == 2
    assert cache.exists(), "expected canary cache file to be written"
    payload = json.loads(cache.read_text())
    assert payload["version"] == CANARY_CACHE_VERSION
    persisted_fragments = {q["gold_fragment"] for q in payload["queries"]}
    assert persisted_fragments == {"foo", "bar"}


@pytest.mark.unit
def test_second_call_loads_from_cache_and_ignores_corpus(tmp_path: Path) -> None:
    """Once cached, subsequent calls load the same queries even if the corpus changes.

    This is the load-bearing contract: the run-over-run delta is only
    meaningful when the same query set is fired each time.
    """
    cache = tmp_path / "canaries.json"
    db1 = _make_documents_db([("docs/architecture.md", "architecture", 1)])
    first = _checker().check(db=db1, canary_cache_path=cache)
    first_fragments = {d["gold_fragment"] for d in first["detail"]}

    # New DB with a completely different document set — second call must
    # still fire the originally-cached queries.
    db2 = _make_documents_db([("docs/totally-different.md", "totally-different", 1)])
    second = _checker().check(db=db2, canary_cache_path=cache)
    second_fragments = {d["gold_fragment"] for d in second["detail"]}

    assert second_fragments == first_fragments, "cache load must override DB sampling on subsequent calls"
    assert second_fragments == {"architecture"}


@pytest.mark.unit
def test_rebuild_canaries_true_discards_cache_and_resamples(tmp_path: Path) -> None:
    """Explicit ``rebuild_canaries=True`` ignores the cache and re-samples."""
    cache = tmp_path / "canaries.json"
    db1 = _make_documents_db([("docs/old.md", "old", 1)])
    _checker().check(db=db1, canary_cache_path=cache)

    db2 = _make_documents_db([("docs/new.md", "new", 1)])
    rebuilt = _checker().check(db=db2, canary_cache_path=cache, rebuild_canaries=True)
    rebuilt_fragments = {d["gold_fragment"] for d in rebuilt["detail"]}

    assert rebuilt_fragments == {"new"}, "rebuild_canaries=True must sample from current DB"
    payload = json.loads(cache.read_text())
    assert payload["queries"][0]["gold_fragment"] == "new"


@pytest.mark.unit
def test_corrupt_cache_file_is_ignored_and_rebuilt(tmp_path: Path) -> None:
    """A truncated/invalid cache JSON does not crash the loader."""
    cache = tmp_path / "canaries.json"
    cache.write_text("{not valid json", encoding="utf-8")

    db = _make_documents_db([("docs/x.md", "x", 1)])
    result = _checker().check(db=db, canary_cache_path=cache)

    assert result["total"] == 1, "loader must skip corrupt cache and fall through to sampling"
    payload = json.loads(cache.read_text())
    assert payload["version"] == CANARY_CACHE_VERSION


@pytest.mark.unit
def test_schema_mismatched_cache_is_rejected(tmp_path: Path) -> None:
    """Future-version or hand-edited caches with the wrong schema rebuild."""
    cache = tmp_path / "canaries.json"
    cache.write_text(
        json.dumps({"version": 999, "queries": [{"id": "X", "query": "q", "gold_fragment": "g"}]}),
        encoding="utf-8",
    )

    db = _make_documents_db([("docs/y.md", "y", 1)])
    result = _checker().check(db=db, canary_cache_path=cache)

    fragments = {d["gold_fragment"] for d in result["detail"]}
    assert fragments == {"y"}, "future-version cache must be discarded"
    payload = json.loads(cache.read_text())
    assert payload["version"] == CANARY_CACHE_VERSION


@pytest.mark.unit
def test_empty_corpus_returns_defaults_and_does_not_write_cache(tmp_path: Path) -> None:
    """Static defaults are not cached — they don't represent a corpus snapshot."""
    cache = tmp_path / "canaries.json"
    db = _make_documents_db([])

    result = _checker().check(db=db, canary_cache_path=cache)

    assert result["total"] == len(DEFAULT_RECALL_QUERIES)
    assert not cache.exists(), "static defaults must not be cached"


@pytest.mark.unit
def test_load_canary_cache_returns_none_when_file_missing(tmp_path: Path) -> None:
    """Public loader API returns None for missing cache (callers fall through to build)."""
    assert load_canary_cache(tmp_path / "absent.json") is None


@pytest.mark.unit
def test_save_canary_cache_creates_parent_directory(tmp_path: Path) -> None:
    """``save_canary_cache`` creates the cache directory if it does not exist."""
    nested = tmp_path / "nested" / "dir" / "canaries.json"
    save_canary_cache([("X1", "query", "fragment")], nested, corpus_size=42)

    assert nested.exists()
    payload = json.loads(nested.read_text())
    assert payload["corpus_size_at_creation"] == 42
    assert payload["queries"][0]["id"] == "X1"


@pytest.mark.unit
def test_load_canary_cache_round_trips_what_save_canary_cache_writes(tmp_path: Path) -> None:
    """The public load/save pair is symmetric: load returns the same triples save accepted."""
    cache = tmp_path / "canaries.json"
    queries = [("Q1", "first query", "first"), ("Q2", "second query", "second")]
    save_canary_cache(queries, cache, corpus_size=7)

    loaded = load_canary_cache(cache)
    assert loaded == queries


@pytest.mark.unit
def test_cache_path_none_bypasses_cache_entirely(tmp_path: Path) -> None:
    """``canary_cache_path=None`` is the test/operator escape hatch — never reads, never writes."""
    db1 = _make_documents_db([("docs/a.md", "a", 1)])
    first = _checker().check(db=db1, canary_cache_path=None)

    db2 = _make_documents_db([("docs/b.md", "b", 1)])
    second = _checker().check(db=db2, canary_cache_path=None)

    assert {d["gold_fragment"] for d in first["detail"]} == {"a"}
    assert {d["gold_fragment"] for d in second["detail"]} == {"b"}
