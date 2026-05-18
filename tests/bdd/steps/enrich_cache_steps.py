"""Step definitions for enrich_cache.feature.

Exercises ``SQLiteDocumentRepository``'s chunk-date LRU cache against a
real on-disk SQLite DB (no @patch, no monkeypatching of internals). The
SQL backend call count is observed via the ``cache_info()`` of the
``functools.lru_cache`` wrapping ``_get_chunk_dates_uncached`` — ``misses``
is the number of times the SQL backend was actually invoked.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from pytest_bdd import given, parsers, then, when

from kairix.core.db import open_db
from kairix.core.db.repository import SQLiteDocumentRepository
from kairix.core.db.schema import create_schema

# Module-level state, scoped per-scenario by pytest-bdd's fresh module load.
_state: dict = {}


def _split_csv_paths(csv: str) -> list[str]:
    """Parse a comma-separated path list from a feature step."""
    return [p.strip() for p in csv.split(",") if p.strip()]


@given("a document repository with chunk-date enrichment caching")
def repo_with_cache() -> None:
    tmp_dir = tempfile.mkdtemp()
    db_path = Path(tmp_dir) / "enrich-cache.sqlite"
    db = open_db(db_path)
    try:
        create_schema(db)
    finally:
        db.close()
    _state.clear()
    _state["db_path"] = db_path
    _state["repo"] = SQLiteDocumentRepository(db_path=db_path)
    _state["responses"] = []


@given(parsers.parse('documents with chunk dates at paths "{path_a}" and "{path_b}"'))
def seed_two_dated_docs(path_a: str, path_b: str) -> None:
    repo: SQLiteDocumentRepository = _state["repo"]
    db_path: Path = _state["db_path"]

    repo.insert_or_update(path_a, "notes", "A", "alpha body", "h-a")
    repo.insert_or_update(path_b, "notes", "B", "beta body", "h-b")

    db = open_db(db_path)
    try:
        db.execute(
            "INSERT INTO content_vectors (hash, seq, pos, model, embedded_at, chunk_date) VALUES (?, ?, ?, ?, ?, ?)",
            ("h-a", 0, 0, "model", 1000, "2026-05-01"),
        )
        db.execute(
            "INSERT INTO content_vectors (hash, seq, pos, model, embedded_at, chunk_date) VALUES (?, ?, ?, ?, ?, ?)",
            ("h-b", 0, 0, "model", 1000, "2026-05-02"),
        )
        db.commit()
    finally:
        db.close()


@when(parsers.parse('chunk dates are fetched for "{paths}"'))
def fetch_chunk_dates(paths: str) -> None:
    repo: SQLiteDocumentRepository = _state["repo"]
    _state["responses"].append(repo.get_chunk_dates(_split_csv_paths(paths)))


@when(parsers.parse('chunk dates are fetched again for "{paths}"'))
def fetch_chunk_dates_again(paths: str) -> None:
    repo: SQLiteDocumentRepository = _state["repo"]
    _state["responses"].append(repo.get_chunk_dates(_split_csv_paths(paths)))


@then(parsers.parse("the SQL backend was called {count:d} time"))
@then(parsers.parse("the SQL backend was called {count:d} times"))
def assert_sql_call_count(count: int) -> None:
    repo: SQLiteDocumentRepository = _state["repo"]
    info = repo._chunk_dates_cache.cache_info()
    # ``misses`` increments each time the cache routes to the SQL helper.
    assert info.misses == count, (
        f"Expected {count} SQL-backend calls, got {info.misses} (hits={info.hits}, currsize={info.currsize})"
    )


@then("the cache returned the same chunk dates for both calls")
def assert_same_chunk_dates() -> None:
    responses = _state["responses"]
    assert len(responses) == 2, f"Expected 2 responses, got {len(responses)}"
    # Identity equality proves a cache hit, not just structural equality.
    assert responses[0] is responses[1]


@then("the two cached entries were independent")
def assert_two_independent_entries() -> None:
    responses = _state["responses"]
    assert len(responses) == 2, f"Expected 2 responses, got {len(responses)}"
    assert responses[0] is not responses[1]
    # And each contains only its own path's chunk date.
    assert len(responses[0]) <= 1
    assert len(responses[1]) <= 1
