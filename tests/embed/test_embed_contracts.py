"""Contract-first tests for kairix.core.embed.embed — describe what the
docstrings/return shape claim and verify the implementation honours them.

Written from the public contract, NOT from the current code.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest


def _seed_documents(db: sqlite3.Connection, docs: list[tuple[str, str, str]]) -> None:
    db.execute("CREATE TABLE documents (hash TEXT PRIMARY KEY, path TEXT, active INTEGER DEFAULT 1)")
    db.execute("CREATE TABLE content (hash TEXT PRIMARY KEY, doc TEXT)")
    db.execute(
        "CREATE TABLE content_vectors"
        " (hash TEXT, seq INTEGER, pos INTEGER, model TEXT, embedded_at INTEGER, chunk_date TEXT)"
    )
    for content_hash, doc_body, path in docs:
        db.execute("INSERT INTO content (hash, doc) VALUES (?, ?)", (content_hash, doc_body))
        db.execute(
            "INSERT INTO documents (hash, path, active) VALUES (?, ?, 1)",
            (content_hash, path),
        )
    db.commit()


def _build_run_embed_deps(
    *,
    embed_batch: Any = None,
    open_usearch_index: Any = None,
):
    from kairix.core.embed.deps import EmbedDependencies

    return EmbedDependencies(
        get_azure_config=lambda: ("key", "https://ep.com", "deploy"),
        preflight_check=lambda *_a, **_kw: 1536,
        migrate_content_vectors=lambda _db: None,
        open_usearch_index=open_usearch_index or (lambda: None),
        get_document_root=lambda: None,
        embed_batch=embed_batch or (lambda texts, *_a, **_kw: [[0.1] * 1536 for _ in texts]),
    )


# ---------------------------------------------------------------------------
# Contract: ``force=True`` re-embeds everything — including documents whose
# vectors are already in content_vectors.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_run_embed_force_reembeds_documents_with_existing_vectors() -> None:
    """The ``force`` docstring says "Re-embed everything, not just pending".

    Pre-populate content_vectors with stale rows for an indexed document, then
    run with force=True. The contract: stale rows are cleared and the document
    is re-embedded so content_vectors only has the new rows.
    """
    from kairix.core.embed.embed import run_embed

    db = sqlite3.connect(":memory:")
    _seed_documents(db, [("h1", "doc body content " * 10, "docs/a.md")])

    # Pre-populate a stale vectors row tagged with an old model — represents a
    # previous run that needs to be discarded under force=True.
    db.execute(
        "INSERT INTO content_vectors (hash, seq, pos, model, embedded_at) VALUES (?, ?, ?, ?, ?)",
        ("h1", 0, 0, "old-model", 0),
    )
    db.commit()
    assert db.execute("SELECT COUNT(*) FROM content_vectors WHERE model='old-model'").fetchone()[0] == 1

    deps = _build_run_embed_deps()
    result = run_embed(db, force=True, batch_size=10, deps=deps)

    assert result["embedded"] >= 1
    # The stale row must be gone (force=True clears existing vectors).
    stale_remaining = db.execute("SELECT COUNT(*) FROM content_vectors WHERE model='old-model'").fetchone()[0]
    assert stale_remaining == 0
    # And a fresh row exists with the new model.
    fresh_rows = db.execute("SELECT COUNT(*) FROM content_vectors WHERE model='deploy'").fetchone()[0]
    assert fresh_rows >= 1


# ---------------------------------------------------------------------------
# Contract: ``limit`` caps total chunks processed.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_run_embed_limit_caps_chunks_processed_below_total_available() -> None:
    """``limit`` per docstring: "Cap total chunks (for validation/testing)".

    Seed enough documents that gather_pending_chunks would yield > limit
    chunks, then assert exactly ``limit`` chunks were embedded.
    """
    from kairix.core.embed.embed import run_embed

    db = sqlite3.connect(":memory:")
    # Three short docs → three chunks (each fits in a single chunk_text bucket).
    _seed_documents(
        db,
        [
            ("h1", "first doc body " * 5, "docs/a.md"),
            ("h2", "second doc body " * 5, "docs/b.md"),
            ("h3", "third doc body " * 5, "docs/c.md"),
        ],
    )

    deps = _build_run_embed_deps()
    result = run_embed(db, limit=2, batch_size=10, deps=deps)

    # Exactly 2 chunks embedded (limit), not 3.
    assert result["embedded"] == 2, f"expected limit=2 to cap embedded count; got {result['embedded']}"


# ---------------------------------------------------------------------------
# Contract: returned dict shape — keys per docstring are
# embedded, skipped, failed, duration_s, estimated_cost_usd.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_run_embed_result_dict_carries_every_documented_key() -> None:
    """The docstring promises a dict with: embedded, skipped, failed,
    duration_s, estimated_cost_usd. The result must carry all five.
    """
    from kairix.core.embed.embed import run_embed

    db = sqlite3.connect(":memory:")
    _seed_documents(db, [("h1", "body " * 10, "docs/a.md")])

    result = run_embed(db, batch_size=10, deps=_build_run_embed_deps())
    expected_keys = {"embedded", "skipped", "failed", "duration_s", "estimated_cost_usd"}
    assert expected_keys.issubset(set(result.keys())), (
        f"result missing documented keys; expected {expected_keys}, got {set(result.keys())}"
    )


# ---------------------------------------------------------------------------
# Contract: a partial response from embed_batch (fewer vectors than texts)
# must NOT silently report all chunks as embedded — those without a vector
# are not staged in content_vectors and should not be counted as embedded.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_run_embed_does_not_overcount_when_embed_batch_returns_fewer_vectors_than_texts() -> None:
    """If the embed backend returns fewer vectors than texts (partial response),
    the unmatched chunks must not be silently counted as embedded.

    Contract: ``embedded`` reflects the number of chunks that actually got a
    vector and were staged in content_vectors. Anything else surfaces as
    ``failed`` or ``skipped`` (per the docstring's documented keys).
    """
    from kairix.core.embed.embed import run_embed

    db = sqlite3.connect(":memory:")
    _seed_documents(
        db,
        [
            ("h1", "first body " * 5, "docs/a.md"),
            ("h2", "second body " * 5, "docs/b.md"),
            ("h3", "third body " * 5, "docs/c.md"),
        ],
    )

    def _partial_embed_batch(texts, *_a, **_kw):
        # Pretend the backend dropped one of the three texts on the floor —
        # returns 2 vectors for 3 texts. zip(strict=False) silently truncates.
        return [[0.1] * 1536 for _ in texts[:-1]]

    deps = _build_run_embed_deps(embed_batch=_partial_embed_batch)
    result = run_embed(db, batch_size=10, deps=deps)

    staged_count = db.execute("SELECT COUNT(*) FROM content_vectors").fetchone()[0]
    # Whatever embedded reports, content_vectors is the source of truth — they
    # must agree. If they don't, the chunk that didn't get a vector was
    # silently miscounted.
    assert result["embedded"] == staged_count, (
        f"embedded={result['embedded']} but content_vectors has {staged_count} rows — "
        "partial-response chunks were miscounted"
    )
