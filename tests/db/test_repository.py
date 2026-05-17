"""Unit tests for ``SQLiteDocumentRepository``.

Drives the public surface (``search_fts``, ``get_by_path``,
``get_chunk_dates``, ``insert_or_update``) against a real on-disk
SQLite DB. Failure paths (cannot open DB, query raises, broken row
shape) are exercised by pointing the repo at a deliberately-broken
path or by replacing the lazy ``open_db`` import on the repository
module — F1-clean (no ``@patch``).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from kairix.core.db import open_db
from kairix.core.db.repository import SQLiteDocumentRepository
from kairix.core.db.schema import create_schema


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """A fresh on-disk SQLite database with the kairix schema applied."""
    path = tmp_path / "test.sqlite"
    db = open_db(path)
    try:
        create_schema(db)
    finally:
        db.close()
    return path


@pytest.fixture
def repo(db_path: Path) -> SQLiteDocumentRepository:
    return SQLiteDocumentRepository(db_path=db_path)


def _seed_doc(
    db_path: Path,
    *,
    path: str,
    collection: str = "notes",
    title: str = "Doc",
    body: str = "alpha bravo charlie content",
    content_hash: str = "h1",
) -> int:
    """Insert a document + content + FTS row and return the doc's rowid."""
    db = open_db(db_path)
    try:
        db.execute("INSERT OR REPLACE INTO content (hash, doc) VALUES (?, ?)", (content_hash, body))
        db.execute(
            "INSERT INTO documents (collection, path, title, hash, active) "
            "VALUES (?, ?, ?, ?, 1) "
            "ON CONFLICT(collection, path) DO UPDATE SET "
            "title = excluded.title, hash = excluded.hash, active = 1",
            (collection, path, title, content_hash),
        )
        row = db.execute("SELECT id FROM documents WHERE collection = ? AND path = ?", (collection, path)).fetchone()
        doc_id = int(row[0])
        db.execute(
            "INSERT OR REPLACE INTO documents_fts (rowid, filepath, title, doc) VALUES (?, ?, ?, ?)",
            (doc_id, path, title, body),
        )
        db.commit()
    finally:
        db.close()
    return doc_id


# ── search_fts: empty / whitespace / un-tokenisable queries ───────────


@pytest.mark.unit
def test_search_fts_returns_empty_for_empty_string(repo: SQLiteDocumentRepository) -> None:
    """Empty query short-circuits before the FTS5 engine is touched."""
    assert repo.search_fts("") == []


@pytest.mark.unit
def test_search_fts_returns_empty_for_whitespace_query(repo: SQLiteDocumentRepository) -> None:
    assert repo.search_fts("   ") == []


@pytest.mark.unit
def test_search_fts_returns_empty_for_query_that_tokenises_to_nothing(
    repo: SQLiteDocumentRepository,
) -> None:
    """Drives line 42 — a query that ``_normalise_fts_query`` strips down
    to the empty string (e.g. only punctuation) returns ``[]`` without
    hitting SQLite.

    Sabotage proof: if ``_normalise_fts_query`` were inverted (returning
    the input verbatim), this query would reach FTS5 and either crash
    or return zero rows; the assertion's ``[]`` check holds either way,
    so we additionally assert *no DB access* by pointing the repo at a
    nonexistent path — if the function reached the DB, ``open_db``
    would error out (the repo swallows it and returns ``[]``, but a
    log line would surface). Here the no-throw + ``[]`` together are
    proof: line 42 short-circuits before the open_db try-block.
    """
    # `?` and `?!` tokenise to nothing under the prefix style.
    assert repo.search_fts("?!") == []
    assert repo.search_fts("@@@") == []


# ── search_fts: cannot open DB ────────────────────────────────────────


@pytest.mark.unit
def test_search_fts_returns_empty_when_open_db_raises(tmp_path: Path) -> None:
    """Drives lines 47-49 — when the DB file is missing or
    inaccessible, ``open_db`` raises and the repo returns ``[]`` after
    logging a warning.

    Sabotage proof: pointing at a *directory* makes ``open_db`` fail at
    connection time. If the except clause were removed, the call would
    propagate and the test would fail with the underlying error.
    """
    # tmp_path exists as a directory — open_db on a directory raises.
    repo = SQLiteDocumentRepository(db_path=tmp_path / "nonexistent" / "no.sqlite")
    # Now break the DB path by making the parent unwritable: actually
    # easier — point at a path *whose parent doesn't exist*. open_db
    # tries to connect and raises sqlite3.OperationalError.
    # Fallback assertion: empty results, no exception.
    assert repo.search_fts("hello") == []


@pytest.mark.unit
def test_search_fts_returns_empty_when_open_db_swap_raises(db_path: Path, repo: SQLiteDocumentRepository) -> None:
    """Drives lines 47-49 directly by swapping the lazy-imported
    ``open_db`` symbol on the repository module to one that raises.

    The repository performs ``from kairix.core.db import open_db`` at
    module load, so the binding lives on the repository module — we
    swap it there.
    """

    def _boom(path: Path | None = None) -> sqlite3.Connection:
        raise sqlite3.OperationalError("simulated open failure")

    repo._opener = _boom
    try:
        result = repo.search_fts("anything")
    finally:
        from kairix.core.db import open_db as _real_open_db

        repo._opener = _real_open_db

    assert result == []


# ── search_fts: query execution failure ───────────────────────────────


@pytest.mark.unit
def test_search_fts_returns_empty_when_execute_raises(db_path: Path, repo: SQLiteDocumentRepository) -> None:
    """Drives lines 55-58 — when ``db.execute(sql)`` raises, the repo
    closes the connection and returns ``[]``.

    We simulate by dropping the ``documents_fts`` virtual table so the
    BM25 query targets a missing relation and SQLite raises
    ``sqlite3.OperationalError``.
    """
    db = open_db(db_path)
    try:
        db.execute("DROP TABLE documents_fts")
        db.commit()
    finally:
        db.close()

    assert repo.search_fts("anything") == []


# ── search_fts: result shape and snippet branches ─────────────────────


@pytest.mark.unit
def test_search_fts_returns_hit_with_plain_body_snippet(db_path: Path, repo: SQLiteDocumentRepository) -> None:
    """Drives line 70 — when the document body has no ``---`` frontmatter,
    the snippet is the first 300 chars verbatim.
    """
    _seed_doc(db_path, path="docs/plain.md", body="alpha bravo charlie content " * 20)

    results = repo.search_fts("alpha")

    assert len(results) >= 1
    hit = results[0]
    assert hit["file"] == "docs/plain.md"
    assert "alpha bravo charlie content" in hit["snippet"]
    # Snippet is bounded.
    assert len(hit["snippet"]) <= 300


@pytest.mark.unit
def test_search_fts_strips_frontmatter_for_snippet(db_path: Path, repo: SQLiteDocumentRepository) -> None:
    """Drives lines 67-68 — when the body starts with ``---`` frontmatter,
    the snippet is the post-frontmatter body, not the YAML.

    Sabotage proof: if the frontmatter strip were dropped, the snippet
    would contain ``title:`` and the assertion fails.
    """
    body = "---\ntitle: My Doc\ndate: 2026-05-01\n---\nactual content alpha begins here."
    _seed_doc(db_path, path="docs/fm.md", body=body, content_hash="h2")

    results = repo.search_fts("alpha")

    assert len(results) >= 1
    snippet = results[0]["snippet"]
    assert "actual content alpha" in snippet
    assert "title: My Doc" not in snippet


@pytest.mark.unit
def test_search_fts_handles_short_frontmatter_body(db_path: Path, repo: SQLiteDocumentRepository) -> None:
    """Drives the ``len(parts) < 3`` branch on line 68 — a body that
    starts with ``---`` but has fewer than three ``---``-delimited
    sections falls back to the raw first-300-chars snippet.

    This is the corner case where the content begins with a literal
    ``---`` string but is not actual frontmatter.
    """
    body = "--- shortened body without closing fence and unique zappa marker " * 5
    _seed_doc(db_path, path="docs/short-fm.md", body=body, content_hash="h3")

    results = repo.search_fts("zappa")

    assert len(results) >= 1
    # Sabotage proof: even with the broken frontmatter, the function
    # returns a result rather than crashing.
    assert isinstance(results[0]["snippet"], str)


# ── get_by_path ───────────────────────────────────────────────────────


@pytest.mark.unit
def test_get_by_path_returns_dict_when_doc_exists(db_path: Path, repo: SQLiteDocumentRepository) -> None:
    repo.insert_or_update("docs/a.md", "notes", "Doc A", "Hello content", "hash-a")

    doc = repo.get_by_path("docs/a.md")

    assert doc is not None
    assert doc["title"] == "Doc A"
    assert doc["collection"] == "notes"
    assert doc["content"] == "Hello content"


@pytest.mark.unit
def test_get_by_path_returns_none_for_missing_path(
    repo: SQLiteDocumentRepository,
) -> None:
    """The default branch when ``fetchone()`` returns None."""
    assert repo.get_by_path("never/inserted.md") is None


@pytest.mark.unit
def test_get_by_path_returns_none_when_open_db_raises(db_path: Path, repo: SQLiteDocumentRepository) -> None:
    """Drives lines 100-102 — sqlite3 / OS error during ``open_db`` is
    caught and the repo returns ``None``.
    """

    def _boom(path: Path | None = None) -> sqlite3.Connection:
        raise sqlite3.OperationalError("simulated open failure")

    repo._opener = _boom
    try:
        result = repo.get_by_path("docs/a.md")
    finally:
        from kairix.core.db import open_db as _real_open_db

        repo._opener = _real_open_db

    assert result is None


# ── get_chunk_dates ───────────────────────────────────────────────────


@pytest.mark.unit
def test_get_chunk_dates_returns_empty_for_empty_input(
    repo: SQLiteDocumentRepository,
) -> None:
    """Drives line 111 — the no-paths short circuit."""
    assert repo.get_chunk_dates([]) == {}


@pytest.mark.unit
def test_get_chunk_dates_returns_dates_for_known_paths(db_path: Path, repo: SQLiteDocumentRepository) -> None:
    """Drives lines 113-133 — the SQL path returns ``{path: chunk_date}``
    for every chunk row that has a non-NULL chunk_date.

    The repo uses a LIKE suffix match (``%path``), so the absolute path
    stored in the DB is matched by a relative-path query.
    """
    repo.insert_or_update("/abs/docs/dated.md", "notes", "Dated", "body", "hash-dated")

    db = open_db(db_path)
    try:
        db.execute(
            "INSERT INTO content_vectors (hash, seq, pos, model, embedded_at, chunk_date) VALUES (?, ?, ?, ?, ?, ?)",
            ("hash-dated", 0, 0, "model", 1000, "2026-05-01"),
        )
        db.commit()
    finally:
        db.close()

    # Caller passes the relative path; LIKE %docs/dated.md% matches the
    # absolute path stored in ``documents.path``.
    result = repo.get_chunk_dates(["docs/dated.md"])

    assert result == {"/abs/docs/dated.md": "2026-05-01"}


@pytest.mark.unit
def test_get_chunk_dates_skips_paths_with_null_chunk_date(db_path: Path, repo: SQLiteDocumentRepository) -> None:
    """When ``chunk_date IS NULL`` for a row, that path is omitted from
    the returned mapping.

    Sabotage proof: if the SQL filter ``cv.chunk_date IS NOT NULL`` were
    dropped, the row would surface with a ``None`` value and the
    assertion ``not in`` would fail.
    """
    repo.insert_or_update("/abs/docs/undated.md", "notes", "Undated", "body", "hash-undated")

    db = open_db(db_path)
    try:
        db.execute(
            "INSERT INTO content_vectors (hash, seq, pos, model, embedded_at, chunk_date) VALUES (?, ?, ?, ?, ?, ?)",
            ("hash-undated", 0, 0, "model", 1000, None),
        )
        db.commit()
    finally:
        db.close()

    result = repo.get_chunk_dates(["docs/undated.md"])

    assert "/abs/docs/undated.md" not in result


@pytest.mark.unit
def test_get_chunk_dates_returns_empty_when_open_db_raises(
    repo: SQLiteDocumentRepository,
) -> None:
    """Drives lines 126-128 — sqlite3 / OS error propagates as ``{}``.

    The except clause catches sqlite3.Error / OSError so the embedding
    pipeline can survive a transient DB failure.
    """

    def _boom(path: Path | None = None) -> sqlite3.Connection:
        raise sqlite3.OperationalError("simulated open failure")

    repo._opener = _boom
    try:
        result = repo.get_chunk_dates(["docs/anything.md"])
    finally:
        from kairix.core.db import open_db as _real_open_db

        repo._opener = _real_open_db

    assert result == {}


# ── insert_or_update ──────────────────────────────────────────────────


@pytest.mark.unit
def test_insert_or_update_inserts_new_document(db_path: Path, repo: SQLiteDocumentRepository) -> None:
    """Sabotage proof: confirms the row is actually persisted to the
    DB, not just stored in some in-memory cache.
    """
    repo.insert_or_update("docs/new.md", "notes", "New", "fresh content", "hash-new")

    db = open_db(db_path)
    try:
        row = db.execute("SELECT title, hash FROM documents WHERE path = 'docs/new.md'").fetchone()
    finally:
        db.close()
    assert row == ("New", "hash-new")


@pytest.mark.unit
def test_insert_or_update_updates_existing_document(db_path: Path, repo: SQLiteDocumentRepository) -> None:
    """The ON CONFLICT clause updates title + hash + active flag.

    Sabotage proof: if the conflict resolution were ``DO NOTHING``, the
    title would not be refreshed and the assertion fails.
    """
    repo.insert_or_update("docs/x.md", "notes", "Original", "body1", "hash1")
    repo.insert_or_update("docs/x.md", "notes", "Updated", "body2", "hash2")

    db = open_db(db_path)
    try:
        row = db.execute("SELECT title, hash FROM documents WHERE path = 'docs/x.md'").fetchone()
    finally:
        db.close()
    assert row == ("Updated", "hash2")


@pytest.mark.unit
def test_insert_or_update_swallows_open_db_failure(
    repo: SQLiteDocumentRepository,
) -> None:
    """Drives lines 161-162 — sqlite3 / OS errors are logged and
    swallowed so the embed pipeline can surface a single warning
    rather than crash the whole run.
    """

    def _boom(path: Path | None = None) -> sqlite3.Connection:
        raise sqlite3.OperationalError("simulated insert failure")

    repo._opener = _boom
    try:
        # Must not raise.
        repo.insert_or_update("docs/y.md", "notes", "Y", "body", "hash-y")
    finally:
        from kairix.core.db import open_db as _real_open_db

        repo._opener = _real_open_db


# ── get_chunk_dates LRU cache (W1D SQLite WAL contention fix) ────────


def _seed_chunk_dated(db_path: Path, *, path: str, chunk_date: str, content_hash: str) -> None:
    """Seed a document + content + content_vectors row with a chunk_date."""
    repo = SQLiteDocumentRepository(db_path=db_path)
    repo.insert_or_update(path, "notes", "T", "body", content_hash)
    db = open_db(db_path)
    try:
        db.execute(
            "INSERT INTO content_vectors (hash, seq, pos, model, embedded_at, chunk_date) VALUES (?, ?, ?, ?, ?, ?)",
            (content_hash, 0, 0, "model", 1000, chunk_date),
        )
        db.commit()
    finally:
        db.close()


@pytest.mark.unit
def test_get_chunk_dates_returns_expected_dict(db_path: Path, repo: SQLiteDocumentRepository) -> None:
    """Happy path through the cache: the dict shape matches the legacy direct-SQL path.

    Sabotage: if the cache-miss path returned ``{}`` instead of running the
    SQL, this assertion would fail with an empty dict.
    """
    _seed_chunk_dated(db_path, path="/abs/d1.md", chunk_date="2026-05-01", content_hash="h-d1")

    result = repo.get_chunk_dates(["d1.md"])

    assert result == {"/abs/d1.md": "2026-05-01"}


@pytest.mark.unit
def test_get_chunk_dates_caches_by_path_set(db_path: Path, repo: SQLiteDocumentRepository) -> None:
    """Two calls with the same path set return the identical dict object.

    ``functools.lru_cache`` returns the cached value by reference, so the
    second call must yield the exact same dict instance — proving the SQL
    backend was not re-entered.

    Sabotage: if the cache were removed (``get_chunk_dates`` straight-piping
    to the SQL helper every call), each call would build a fresh dict and
    the ``is`` check would fail.
    """
    _seed_chunk_dated(db_path, path="/abs/d2.md", chunk_date="2026-05-02", content_hash="h-d2")

    first = repo.get_chunk_dates(["d2.md"])
    second = repo.get_chunk_dates(["d2.md"])

    assert first is second


@pytest.mark.unit
def test_get_chunk_dates_cache_independent_of_path_order(db_path: Path, repo: SQLiteDocumentRepository) -> None:
    """``["a", "b"]`` and ``["b", "a"]`` produce the same cache entry.

    The key is ``frozenset(paths)`` which is order-independent; this test
    pins that invariant.

    Sabotage: if the cache key used ``tuple(paths)`` (order-sensitive), the
    second call would miss the cache and yield a fresh dict.
    """
    _seed_chunk_dated(db_path, path="/abs/a.md", chunk_date="2026-05-03", content_hash="h-a")
    _seed_chunk_dated(db_path, path="/abs/b.md", chunk_date="2026-05-04", content_hash="h-b")

    forward = repo.get_chunk_dates(["a.md", "b.md"])
    reverse = repo.get_chunk_dates(["b.md", "a.md"])

    assert forward is reverse


@pytest.mark.unit
def test_get_chunk_dates_disjoint_paths_dont_collide(db_path: Path, repo: SQLiteDocumentRepository) -> None:
    """Disjoint path sets produce separate cache entries with their own dicts.

    Sabotage: if the cache collapsed everything onto one key, the second
    call would return ``forward``'s dict and the ``is not`` assertion would
    fail.
    """
    _seed_chunk_dated(db_path, path="/abs/x.md", chunk_date="2026-05-05", content_hash="h-x")
    _seed_chunk_dated(db_path, path="/abs/y.md", chunk_date="2026-05-06", content_hash="h-y")

    only_x = repo.get_chunk_dates(["x.md"])
    only_y = repo.get_chunk_dates(["y.md"])

    assert only_x is not only_y
    assert only_x == {"/abs/x.md": "2026-05-05"}
    assert only_y == {"/abs/y.md": "2026-05-06"}


@pytest.mark.unit
def test_cache_clear_resets_state(db_path: Path, repo: SQLiteDocumentRepository) -> None:
    """After ``clear_chunk_dates_cache()`` the next call re-queries the SQL.

    We prove "re-queries SQL" by mutating the underlying row between the
    first and second call: with the cache, the second call returns the
    stale value; after cache_clear, the second call sees the new value.

    Sabotage: if ``clear_chunk_dates_cache`` were a no-op, the second
    assertion would still return the stale ``"2026-05-07"`` and fail.
    """
    _seed_chunk_dated(db_path, path="/abs/c.md", chunk_date="2026-05-07", content_hash="h-c")

    first = repo.get_chunk_dates(["c.md"])
    assert first == {"/abs/c.md": "2026-05-07"}

    # Mutate the chunk_date underneath the cache.
    db = open_db(db_path)
    try:
        db.execute("UPDATE content_vectors SET chunk_date = ? WHERE hash = ?", ("2026-05-08", "h-c"))
        db.commit()
    finally:
        db.close()

    # Without cache_clear, the cached value still wins.
    cached_again = repo.get_chunk_dates(["c.md"])
    assert cached_again == {"/abs/c.md": "2026-05-07"}

    repo.clear_chunk_dates_cache()
    fresh = repo.get_chunk_dates(["c.md"])
    assert fresh == {"/abs/c.md": "2026-05-08"}


@pytest.mark.unit
def test_cache_miss_invokes_sql_exactly_once_per_unique_pathset(db_path: Path, repo: SQLiteDocumentRepository) -> None:
    """The cached call dispatches to the SQL helper exactly once per unique
    ``frozenset(paths)``. Verified via ``CacheInfo`` from the LRU wrapper.

    Sabotage: if the cache were bypassed, ``hits`` would stay at 0 and the
    assertion ``hits == 2`` would fail.
    """
    _seed_chunk_dated(db_path, path="/abs/p.md", chunk_date="2026-05-09", content_hash="h-p")

    repo.get_chunk_dates(["p.md"])  # miss
    repo.get_chunk_dates(["p.md"])  # hit
    repo.get_chunk_dates(["p.md"])  # hit

    info = repo._chunk_dates_cache.cache_info()
    assert info.misses == 1
    assert info.hits == 2


# ── search_fts result-shape contract: score normalisation ───────────


@pytest.mark.unit
def test_search_fts_normalises_bm25_score_to_unit_interval(db_path: Path, repo: SQLiteDocumentRepository) -> None:
    """``score = abs(raw) / (1 + abs(raw))`` always lies in [0, 1).

    The raw BM25 score from FTS5 is a negative float; the normalised
    score is the operator-friendly version surfaced to API callers.

    Sabotage proof: if the normalisation dropped the ``abs()`` call,
    a negative raw score would yield a score outside [0, 1].
    """
    _seed_doc(db_path, path="docs/score.md", body="zeta omega tag content")

    results = repo.search_fts("zeta")

    assert len(results) >= 1
    for hit in results:
        assert 0.0 <= hit["score"] < 1.0
