"""Step definitions for embed_run.feature.

Drives ``run_embed`` through ``EmbedDependencies``-injected fakes — no
@patch on ``openai.*`` or filesystem state. Each scenario builds a fresh
in-memory SQLite, seeds documents, configures the fake embed backend,
runs the pipeline, and asserts on the returned dict + DB state.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

pytestmark = pytest.mark.bdd


@pytest.fixture(autouse=True)
def _embed_state() -> dict[str, Any]:
    """Per-scenario fresh state."""
    return {
        "db": None,
        "embed_calls": 0,
        "embed_responder": None,
        "preflight_dims": 1536,
        "force": False,
        "limit": None,
        "result": None,
        "exception": None,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_db(state: dict[str, Any], n_docs: int) -> None:
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE documents (hash TEXT PRIMARY KEY, path TEXT, active INTEGER DEFAULT 1)")
    db.execute("CREATE TABLE content (hash TEXT PRIMARY KEY, doc TEXT)")
    db.execute(
        "CREATE TABLE content_vectors"
        " (hash TEXT, seq INTEGER, pos INTEGER, model TEXT, embedded_at INTEGER, chunk_date TEXT)"
    )
    for i in range(n_docs):
        body = f"document {i} content " * 5
        db.execute("INSERT INTO content (hash, doc) VALUES (?, ?)", (f"h{i}", body))
        db.execute(
            "INSERT INTO documents (hash, path, active) VALUES (?, ?, 1)",
            (f"h{i}", f"docs/doc{i}.md"),
        )
    db.commit()
    state["db"] = db


def _build_default_embed(state: dict[str, Any]):
    """Default embed_batch fake: returns one 1536-dim vector per text."""

    def _embed(texts, *_args, **_kwargs):
        state["embed_calls"] += 1
        return [[0.1] * 1536 for _ in texts]

    return _embed


def _build_deps(state: dict[str, Any]):
    from kairix.core.embed.deps import EmbedDependencies

    embed_fn = state["embed_responder"] or _build_default_embed(state)
    return EmbedDependencies(
        get_azure_config=lambda: ("key", "https://ep.com", "deploy"),
        preflight_check=lambda *_a, **_kw: state["preflight_dims"],
        migrate_content_vectors=lambda _db: None,
        open_usearch_index=lambda: None,
        get_document_root=lambda: None,
        embed_batch=embed_fn,
    )


# ---------------------------------------------------------------------------
# Background
# ---------------------------------------------------------------------------


@given("an injected EmbedDependencies wired with deterministic fakes")
def _given_deps(_embed_state: dict[str, Any]) -> None:
    # The Background just declares intent; concrete state is built in subsequent
    # Givens via _build_deps + the per-scenario fakes.
    _embed_state["embed_calls"] = 0


# ---------------------------------------------------------------------------
# Corpus setup
# ---------------------------------------------------------------------------


@given(parsers.parse("a corpus with {n:d} documents and a healthy embed backend"))
def _given_corpus_healthy(_embed_state: dict[str, Any], n: int) -> None:
    _seed_db(_embed_state, n)


@given(parsers.parse("a corpus with {n:d} documents"))
def _given_corpus(_embed_state: dict[str, Any], n: int) -> None:
    _seed_db(_embed_state, n)


@given("a corpus with no pending documents")
def _given_empty_corpus(_embed_state: dict[str, Any]) -> None:
    _seed_db(_embed_state, 0)


@given(parsers.parse("a corpus with {n:d} documents each producing one chunk"))
def _given_corpus_one_chunk_each(_embed_state: dict[str, Any], n: int) -> None:
    _seed_db(_embed_state, n)


@given("a corpus with 1 document and a stale vector row already in content_vectors")
def _given_corpus_with_stale(_embed_state: dict[str, Any]) -> None:
    _seed_db(_embed_state, 1)
    db: sqlite3.Connection = _embed_state["db"]
    db.execute(
        "INSERT INTO content_vectors (hash, seq, pos, model, embedded_at) VALUES (?, ?, ?, ?, ?)",
        ("h0", 0, 0, "stale-model", 0),
    )
    db.commit()


# ---------------------------------------------------------------------------
# Backend configuration
# ---------------------------------------------------------------------------


@given("an embed backend that raises a transient error")
def _given_raising_backend(_embed_state: dict[str, Any]) -> None:
    def _raises(_texts, *_args, **_kwargs):
        raise OSError("transient API outage")

    _embed_state["embed_responder"] = _raises


@given("an embed backend that returns one fewer vector than texts requested")
def _given_partial_backend(_embed_state: dict[str, Any]) -> None:
    def _partial(texts, *_args, **_kwargs):
        return [[0.1] * 1536 for _ in texts[:-1]]

    _embed_state["embed_responder"] = _partial


@given("a preflight check returning unexpected vector dimensions")
def _given_dim_mismatch_preflight(_embed_state: dict[str, Any]) -> None:
    _embed_state["preflight_dims"] = 512  # not the expected 1536
    # Need at least one document so preflight runs (_gather_pending_chunks
    # short-circuits before preflight when there are no docs).
    _seed_db(_embed_state, 1)


# ---------------------------------------------------------------------------
# When — invoke run_embed
# ---------------------------------------------------------------------------


def _run_pipeline(state: dict[str, Any], *, force: bool = False, limit: int | None = None) -> None:
    from kairix.core.embed.embed import run_embed

    try:
        state["result"] = run_embed(state["db"], force=force, limit=limit, batch_size=10, deps=_build_deps(state))
    except Exception as e:
        state["exception"] = e


@when("the operator runs the embed pipeline")
def _when_run(_embed_state: dict[str, Any]) -> None:
    _run_pipeline(_embed_state)


@when("the operator runs the embed pipeline with force enabled")
def _when_run_force(_embed_state: dict[str, Any]) -> None:
    _run_pipeline(_embed_state, force=True)


@when(parsers.parse("the operator runs the embed pipeline with limit {n:d}"))
def _when_run_with_limit(_embed_state: dict[str, Any], n: int) -> None:
    _run_pipeline(_embed_state, limit=n)


# ---------------------------------------------------------------------------
# Then — observable result + DB state
# ---------------------------------------------------------------------------


@then(parsers.parse("the result reports embedded as {n:d}"))
def _then_embedded(_embed_state: dict[str, Any], n: int) -> None:
    assert _embed_state["result"] is not None, f"run_embed raised: {_embed_state.get('exception')}"
    assert _embed_state["result"]["embedded"] == n


@then(parsers.parse("the result reports failed as {n:d}"))
def _then_failed(_embed_state: dict[str, Any], n: int) -> None:
    assert _embed_state["result"]["failed"] == n


@then(parsers.parse("content_vectors contains {n:d} staged rows"))
def _then_staged_rows(_embed_state: dict[str, Any], n: int) -> None:
    db: sqlite3.Connection = _embed_state["db"]
    count = db.execute("SELECT COUNT(*) FROM content_vectors").fetchone()[0]
    assert count == n


@then(parsers.parse("content_vectors contains exactly {n:d} fresh row"))
@then(parsers.parse("content_vectors contains exactly {n:d} fresh rows"))
def _then_fresh_rows(_embed_state: dict[str, Any], n: int) -> None:
    db: sqlite3.Connection = _embed_state["db"]
    fresh = db.execute("SELECT COUNT(*) FROM content_vectors WHERE model = 'deploy'").fetchone()[0]
    assert fresh == n


@then("the embed backend was not invoked")
def _then_backend_not_invoked(_embed_state: dict[str, Any]) -> None:
    assert _embed_state["embed_calls"] == 0


@then("the result's embedded count equals the staged content_vectors count")
def _then_embedded_matches_staged(_embed_state: dict[str, Any]) -> None:
    if _embed_state["exception"] is not None:
        raise AssertionError(
            f"run_embed raised when handling a partial response: "
            f"{type(_embed_state['exception']).__name__}: {_embed_state['exception']} — "
            "the contract is to honestly count, not crash"
        )
    db: sqlite3.Connection = _embed_state["db"]
    staged = db.execute("SELECT COUNT(*) FROM content_vectors").fetchone()[0]
    assert _embed_state["result"]["embedded"] == staged, (
        f"embedded={_embed_state['result']['embedded']} but {staged} rows in content_vectors — "
        "the partial-response chunk was miscounted"
    )


@then("the stale vector row is gone")
def _then_stale_gone(_embed_state: dict[str, Any]) -> None:
    db: sqlite3.Connection = _embed_state["db"]
    stale = db.execute("SELECT COUNT(*) FROM content_vectors WHERE model = 'stale-model'").fetchone()[0]
    assert stale == 0


@then("the embed pipeline raises SchemaVersionError")
def _then_raises_schema_error(_embed_state: dict[str, Any]) -> None:
    from kairix.core.embed.schema import SchemaVersionError

    exc = _embed_state["exception"]
    assert exc is not None and isinstance(exc, SchemaVersionError), (
        f"expected SchemaVersionError; got {type(exc).__name__ if exc else 'no exception'}"
    )
