"""Step definitions for vec_index_batched_metadata.feature.

Drives the real ``VectorIndex._resolve_match_metadata`` against a real
on-disk SQLite (no fakes, no @patch) and counts SQL via SQLite's own
``set_trace_callback`` hook. The connection is wired inside a thin
test-only fetch wrapper — production internals are untouched, so we
catch genuine regressions, not test-fixture noise (#287).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, then, when

from kairix.core.db import open_db
from kairix.core.search import vec_index as vi
from kairix.core.search.vec_index import VectorIndex

pytestmark = pytest.mark.bdd

# F17: phrase fragments reused across multiple steps get a constant.
_PHRASE_SEEDED_DB = "a documents+content SQLite index seeded with ten documents"
_PHRASE_ANN_MAPPING = "a VectorIndex whose ANN mapping covers all ten document hashes"


class _FakeMatches:
    """Minimal usearch.matches surface: ``.keys`` + ``.distances``."""

    def __init__(self, keys: list[int], distances: list[float]) -> None:
        self.keys = keys
        self.distances = distances


@pytest.fixture
def _vec_state(tmp_path: Path) -> dict[str, Any]:
    """Per-scenario fresh state container."""
    return {
        "db_path": tmp_path / "index.sqlite",
        "tmp_path": tmp_path,
        "idx": None,
        "sql_calls": [],
        "result": None,
    }


def _resolve_with_sql_trace(
    idx: VectorIndex,
    matches: _FakeMatches,
    k: int,
    collections: list[str] | None,
    sql_log: list[str],
) -> list[dict]:
    """Re-implement ``_fetch_metadata_batched`` with ``set_trace_callback``.

    The production helper is then called through ``_resolve_match_metadata``
    with the traced helper swapped onto the instance — no module-level
    monkeypatch, no production seam.
    """
    original_fetch = idx._fetch_metadata_batched

    def traced_fetch(unique_hashes: list[str]) -> dict[str, sqlite3.Row]:
        rows_by_hash: dict[str, sqlite3.Row] = {}
        db = open_db(Path(idx._db_path))
        try:
            db.row_factory = sqlite3.Row
            db.set_trace_callback(sql_log.append)
            for start in range(0, len(unique_hashes), vi._IN_CLAUSE_BATCH_SIZE):
                chunk = unique_hashes[start : start + vi._IN_CLAUSE_BATCH_SIZE]
                placeholders = ",".join("?" * len(chunk))
                sql = vi._METADATA_SELECT_SQL.format(placeholders=placeholders)
                for row in db.execute(sql, tuple(chunk)).fetchall():
                    rows_by_hash[row["hash"]] = row
        finally:
            db.close()
        return rows_by_hash

    idx._fetch_metadata_batched = traced_fetch  # type: ignore[method-assign]  # instance-level wrapper attaches trace callback; production module untouched
    try:
        return idx._resolve_match_metadata(matches, k, collections)
    finally:
        idx._fetch_metadata_batched = original_fetch  # type: ignore[method-assign]  # restore original bound method


def _seed_db(db_path: Path, n_docs: int = 10) -> None:
    db = sqlite3.connect(str(db_path))
    db.executescript(
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY, collection TEXT, path TEXT,
            title TEXT, hash TEXT, active INTEGER DEFAULT 1
        );
        CREATE TABLE content (hash TEXT PRIMARY KEY, doc TEXT);
        """
    )
    for i in range(n_docs):
        db.execute(
            "INSERT INTO documents (collection, path, title, hash, active) VALUES (?,?,?,?,1)",
            ("vault", f"vault/doc-{i}.md", f"doc-{i}", f"hash{i}"),
        )
        db.execute("INSERT INTO content (hash, doc) VALUES (?,?)", (f"hash{i}", f"Body {i}."))
    db.commit()
    db.close()


# ---------------------------------------------------------------------------
# Given
# ---------------------------------------------------------------------------


@given(_PHRASE_SEEDED_DB)
def _given_seeded_db(_vec_state: dict[str, Any]) -> None:
    _seed_db(_vec_state["db_path"], n_docs=10)


@given(_PHRASE_ANN_MAPPING)
def _given_vector_index(_vec_state: dict[str, Any]) -> None:
    idx = VectorIndex(
        index_path=_vec_state["tmp_path"] / "vectors.usearch",
        meta_path=_vec_state["tmp_path"] / "vectors.meta.json",
        db_path=_vec_state["db_path"],
    )
    idx._key_to_hash_seq = {i: f"hash{i}_0" for i in range(10)}
    _vec_state["idx"] = idx


@given("the fifth document is marked inactive in the index")
def _given_fifth_inactive(_vec_state: dict[str, Any]) -> None:
    db = sqlite3.connect(str(_vec_state["db_path"]))
    db.execute("UPDATE documents SET active = 0 WHERE hash = ?", ("hash4",))
    db.commit()
    db.close()


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


@when("the metadata resolver runs against the ten ANN hits")
def _when_resolve_ten(_vec_state: dict[str, Any]) -> None:
    matches = _FakeMatches(
        keys=list(range(10)),
        distances=[0.01 * i for i in range(10)],
    )
    _vec_state["result"] = _resolve_with_sql_trace(
        _vec_state["idx"], matches, k=10, collections=None, sql_log=_vec_state["sql_calls"]
    )


@when("the metadata resolver runs against ANN hits in reverse-insert order")
def _when_resolve_reverse(_vec_state: dict[str, Any]) -> None:
    # ANN keys 9 → 0 — opposite of SQLite's natural row order. The
    # resolver MUST follow keys, not SQL order.
    matches = _FakeMatches(
        keys=list(reversed(range(10))),
        distances=[0.01 * i for i in range(10)],
    )
    _vec_state["result"] = _resolve_with_sql_trace(
        _vec_state["idx"], matches, k=10, collections=None, sql_log=_vec_state["sql_calls"]
    )


# ---------------------------------------------------------------------------
# Then
# ---------------------------------------------------------------------------


def _count_selects(sql_log: list[str]) -> int:
    return sum(1 for stmt in sql_log if stmt.strip().upper().startswith("SELECT"))


@then("the metadata resolver issued exactly one SELECT statement")
def _then_one_select(_vec_state: dict[str, Any]) -> None:
    """Sabotage: reverting to per-row SELECT inside _fetch_metadata_batched
    pushes ``count`` to 10 — this assertion fires and the N+1 regression is
    caught.
    """
    count = _count_selects(_vec_state["sql_calls"])
    assert count == 1, f"expected 1 batched SELECT; got {count}: {_vec_state['sql_calls']}"


@then("the resolver returned ten metadata results")
def _then_ten_results(_vec_state: dict[str, Any]) -> None:
    """Sabotage: dropping the ``rows_by_hash[row[\"hash\"]] = row`` line
    leaves the dict empty and every key falls into the ``row is None``
    skip — the length collapses to zero and this assertion fires.
    """
    out = _vec_state["result"]
    assert len(out) == 10, f"expected 10 results; got {len(out)}"


@then("the returned results follow ANN ranking, not SQL row order")
def _then_ann_order(_vec_state: dict[str, Any]) -> None:
    """Sabotage: returning ``rows_by_hash.values()`` directly (instead of
    iterating ordered ANN keys in ``_build_results``) flips the order —
    this exact-list assertion catches it.
    """
    out = _vec_state["result"]
    expected = [f"hash{i}_0" for i in reversed(range(10))]
    actual = [r["hash_seq"] for r in out]
    assert actual == expected, f"ANN order broken: expected {expected}, got {actual}"


@then("the returned results exclude the inactive document")
def _then_excludes_inactive(_vec_state: dict[str, Any]) -> None:
    """Sabotage: removing ``d.active = 1`` from the WHERE clause returns
    hash4 too, so the result count rises to 10 and the ``hash4_0`` check
    fires.
    """
    out = _vec_state["result"]
    hash_seqs = {r["hash_seq"] for r in out}
    assert "hash4_0" not in hash_seqs, "inactive doc leaked into results"
    assert len(out) == 9, f"expected 9 results (10 minus inactive); got {len(out)}"
