"""
Tests for kairix.core.embed.embed — covers paths reachable through the public surface:
- preflight_check(): client=fake injection (no patch on openai SDK)
- stage_embedding(): insert into content_vectors
- batched(): chunk iteration
- chunk_text(): boundary + overlap
- run_embed(): orchestration via EmbedDependencies fakes

``_get_azure_config`` is the production lazy default for credentials and is
now ``# pragma: no cover``-annotated; its credential-resolution behaviour is
exercised in ``tests/test_credentials.py`` against the real
``kairix.credentials.get_credentials("embed")`` boundary. Tests here never
import the private wrapper.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from kairix.core.embed.embed import (
    batched,
    build_hash_seq,
    chunk_text,
    preflight_check,
    stage_embedding,
)

# ---------------------------------------------------------------------------
# Fake OpenAI client for preflight_check — same shape as test_retry.py.
# ---------------------------------------------------------------------------


class _PreflightEmbeddingItem:
    def __init__(self, embedding: list[float]) -> None:
        self.embedding = embedding


class _PreflightResponse:
    def __init__(self, embedding: list[float]) -> None:
        self.data = [_PreflightEmbeddingItem(embedding)]


class _PreflightEmbeddings:
    def __init__(self, responder: Any) -> None:
        self._responder = responder

    def create(self, *, model: str, input: list[str], dimensions: int) -> _PreflightResponse:
        return self._responder()


class _FakePreflightClient:
    """OpenAI-compatible fake whose ``embeddings.create`` returns a fixed-dim vector."""

    def __init__(self, *, dims: int = 1536, raises: BaseException | None = None) -> None:
        if raises is not None:

            def _raiser() -> _PreflightResponse:
                raise raises

            responder = _raiser
        else:

            def _ok() -> _PreflightResponse:
                return _PreflightResponse([0.1] * dims)

            responder = _ok
        self.embeddings = _PreflightEmbeddings(responder)


# ---------------------------------------------------------------------------
# build_hash_seq
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_hash_seq() -> None:
    assert build_hash_seq("abc123", 0) == "abc123_0"
    assert build_hash_seq("abc123", 3) == "abc123_3"


# ---------------------------------------------------------------------------
# chunk_text
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_chunk_text_returns_list_of_dicts() -> None:
    chunks = chunk_text("Hello world. This is a test.", chunk_size=100, overlap=0)
    assert isinstance(chunks, list)
    assert all("seq" in c and "pos" in c and "text" in c for c in chunks)


@pytest.mark.unit
def test_chunk_text_single_chunk_short_text() -> None:
    text = "Short text."
    chunks = chunk_text(text, chunk_size=500, overlap=0)
    assert len(chunks) == 1
    assert chunks[0]["text"] == text
    assert chunks[0]["seq"] == 0
    assert chunks[0]["pos"] == 0


@pytest.mark.unit
def test_chunk_text_multiple_chunks() -> None:
    # Generate text longer than chunk_size
    text = "Word " * 400  # ~2000 chars
    chunks = chunk_text(text, chunk_size=500, overlap=50)
    assert len(chunks) > 1
    # Seq numbers are sequential
    for i, c in enumerate(chunks):
        assert c["seq"] == i


@pytest.mark.unit
def test_chunk_text_empty_string() -> None:
    # Empty string produces one chunk — the function doesn't filter empty results
    result = chunk_text("")
    assert isinstance(result, list)


# ---------------------------------------------------------------------------
# preflight_check — driven through the ``client=`` injection seam.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_preflight_check_returns_embedding_dims_on_success() -> None:
    """A successful embed call returns the length of the returned vector."""
    client = _FakePreflightClient(dims=1536)
    dims = preflight_check("key", "https://fake.example.com", "text-embedding-3-large", client=client)
    assert dims == 1536


@pytest.mark.unit
def test_preflight_check_returns_actual_dims_when_client_returns_smaller_vector() -> None:
    """Sanity check: ``preflight_check`` reports whatever dims the API actually returns."""
    client = _FakePreflightClient(dims=512)
    dims = preflight_check("key", "https://fake.example.com", "text-embedding-3-small", client=client)
    assert dims == 512


@pytest.mark.unit
def test_preflight_check_propagates_authentication_errors_from_client() -> None:
    """AuthenticationError from the client is not swallowed."""
    import openai

    class _StubRequest:
        pass

    class _StubResponse:
        def __init__(self) -> None:
            self.status_code = 401
            self.request = _StubRequest()
            self.headers: dict[str, str] = {}

    err = openai.AuthenticationError(
        message="HTTP 401",
        response=_StubResponse(),
        body=None,
    )
    client = _FakePreflightClient(raises=err)
    with pytest.raises(openai.AuthenticationError):
        preflight_check("bad-key", "https://fake.example.com", "deploy", client=client)


# ---------------------------------------------------------------------------
# stage_embedding
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_stage_embedding_inserts_row() -> None:
    db = sqlite3.connect(":memory:")
    # content_vectors table required by stage_embedding
    db.execute(
        "CREATE TABLE content_vectors"
        " (hash TEXT, seq INTEGER, pos INTEGER, model TEXT, embedded_at INTEGER, chunk_date TEXT)"
    )
    vec = [0.1] * 1536
    stage_embedding(db, "hash123", 0, 0, vec, "text-embedding-3-large", 1711111111)
    count = db.execute("SELECT COUNT(*) FROM content_vectors").fetchone()[0]
    assert count == 1


# ---------------------------------------------------------------------------
# batched
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_batched_splits_correctly() -> None:
    items = list(range(10))
    result = list(batched(items, size=3))
    assert result == [[0, 1, 2], [3, 4, 5], [6, 7, 8], [9]]


@pytest.mark.unit
def test_batched_single_batch() -> None:
    items = [1, 2, 3]
    result = list(batched(items, size=10))
    assert result == [[1, 2, 3]]


@pytest.mark.unit
def test_batched_empty() -> None:
    result = list(batched([], size=5))
    assert result == []


# ---------------------------------------------------------------------------
# chunk_text: sentence boundary path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_chunk_text_splits_on_sentence_boundary() -> None:
    """Chunker prefers sentence boundary when paragraph boundary not available."""
    # Create text long enough to force a split, no double-newline paragraph breaks
    sentence_1 = "This is the first sentence about engineering patterns. "
    sentence_2 = "This is the second sentence about deployment strategies. "
    # Repeat to exceed chunk_size
    text = sentence_1 * 5 + sentence_2 * 5
    chunks = chunk_text(text, chunk_size=200, overlap=0)
    assert len(chunks) > 1
    # All chunks should have text
    for c in chunks:
        assert c["text"].strip()


@pytest.mark.unit
def test_chunk_text_splits_on_paragraph_boundary() -> None:
    """Chunker prefers double-newline paragraph boundary."""
    para_1 = "First paragraph about architecture decisions.\n\n"
    para_2 = "Second paragraph about testing strategy.\n\n"
    text = para_1 * 4 + para_2 * 4
    chunks = chunk_text(text, chunk_size=200, overlap=20)
    assert len(chunks) > 1


# ---------------------------------------------------------------------------
# run_embed: mocked orchestration
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_run_embed_with_no_chunks_returns_stats() -> None:
    """run_embed() with no pending chunks returns embedded=0 immediately."""
    from kairix.core.embed.deps import EmbedDependencies
    from kairix.core.embed.embed import run_embed

    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE documents (hash TEXT PRIMARY KEY, path TEXT, active INTEGER DEFAULT 1)")
    db.execute("CREATE TABLE content (hash TEXT PRIMARY KEY, doc TEXT)")
    db.execute(
        "CREATE TABLE content_vectors"
        " (hash TEXT, seq INTEGER, pos INTEGER, model TEXT, embedded_at INTEGER, chunk_date TEXT)"
    )

    deps = EmbedDependencies(
        get_azure_config=lambda: ("key", "https://ep.com", "deploy"),
        preflight_check=lambda *_a, **_kw: 1536,
        migrate_content_vectors=lambda _db: None,
        open_usearch_index=lambda: None,
        get_document_root=lambda: None,
    )
    result = run_embed(db, batch_size=10, deps=deps)

    assert result["embedded"] == 0
    assert "duration_s" in result
    assert "estimated_cost_usd" in result


@pytest.mark.unit
def test_run_embed_raises_on_dim_mismatch() -> None:
    """run_embed() raises SchemaVersionError when Azure returns unexpected dims."""
    from kairix.core.embed.deps import EmbedDependencies
    from kairix.core.embed.embed import run_embed
    from kairix.core.embed.schema import SchemaVersionError

    db = sqlite3.connect(":memory:")

    deps = EmbedDependencies(
        get_azure_config=lambda: ("key", "https://ep.com", "deploy"),
        preflight_check=lambda *_a, **_kw: 512,  # wrong dims
        migrate_content_vectors=lambda _db: None,
        open_usearch_index=lambda: None,
        get_document_root=lambda: None,
    )
    with pytest.raises(SchemaVersionError):
        run_embed(db, deps=deps)


# ---------------------------------------------------------------------------
# run_embed: vec_index lifecycle + failed_chunks reporting + chunk_date logging
#
# These exercise the four remaining branches that aren't reachable via the
# "zero-chunks happy path" test above:
#   - _save_index_checkpoint success and exception paths
#   - failed_chunks warning when embed_batch raises
#   - chunk_date_populated info log when chunks have dates
# All driven through run_embed(...) — no private-helper imports.
# ---------------------------------------------------------------------------


def _seed_documents(db: sqlite3.Connection, docs: list[tuple[str, str, str]]) -> None:
    """Build the minimum embed-pipeline schema and insert ``(hash, doc_body, path)`` rows."""
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


class _FakeVecIndex:
    """Test double for the usearch index — records add_vectors/save calls.

    Implements the three methods run_embed touches: ``add_vectors(keys, vectors)``,
    ``save()``, and ``__len__``. ``raise_on_save`` flips ``save()`` to raise the
    given exception so the error-path branch in _save_index_checkpoint fires.
    """

    def __init__(self, *, raise_on_save: BaseException | None = None) -> None:
        self.added: list[tuple[list[str], list[list[float]]]] = []
        self.save_calls = 0
        self._raise_on_save = raise_on_save

    def add_vectors(self, keys: list[str], vectors: list[list[float]]) -> None:
        self.added.append((list(keys), [list(v) for v in vectors]))

    def save(self) -> None:
        self.save_calls += 1
        if self._raise_on_save is not None:
            raise self._raise_on_save

    def __len__(self) -> int:
        return sum(len(keys) for keys, _ in self.added)


def _build_run_embed_deps(
    *,
    embed_batch: Any = None,
    open_usearch_index: Any = None,
) -> Any:
    """Build EmbedDependencies with sensible defaults for run_embed tests."""
    from kairix.core.embed.deps import EmbedDependencies

    return EmbedDependencies(
        get_azure_config=lambda: ("key", "https://ep.com", "deploy"),
        preflight_check=lambda *_a, **_kw: 1536,
        migrate_content_vectors=lambda _db: None,
        open_usearch_index=open_usearch_index or (lambda: None),
        get_document_root=lambda: None,
        embed_batch=embed_batch or (lambda texts, *_a, **_kw: [[0.1] * 1536 for _ in texts]),
    )


@pytest.mark.unit
def test_run_embed_saves_vec_index_at_end_and_logs_vector_count(caplog) -> None:
    """When a vec_index is provided, run_embed calls .save() at the end and the
    info log names how many vectors are in the index. Drives the success path
    of _save_index_checkpoint (lines 432-434).
    """
    import logging

    db = sqlite3.connect(":memory:")
    _seed_documents(db, [("h1", "First doc body content. " * 10, "docs/a.md")])

    fake_index = _FakeVecIndex()
    deps = _build_run_embed_deps(open_usearch_index=lambda: fake_index)

    from kairix.core.embed.embed import run_embed

    with caplog.at_level(logging.INFO):
        result = run_embed(db, batch_size=10, deps=deps)

    # Sanity: the run actually processed chunks.
    assert result["embedded"] >= 1
    # The fake index recorded a save call from _save_index_checkpoint.
    assert fake_index.save_calls >= 1
    # The info log embeds the vector count from len(vec_index).
    save_lines = [r.message for r in caplog.records if "saved index with" in r.message]
    assert save_lines, f"expected an 'usearch: saved index' log line; got: {[r.message for r in caplog.records]}"


@pytest.mark.unit
def test_run_embed_logs_error_when_save_index_checkpoint_raises(caplog) -> None:
    """If the vec_index's save() raises at end-of-run, run_embed swallows it
    and emits an error log — never propagates the exception. Drives the
    exception path of _save_index_checkpoint (lines 435-436).
    """
    import logging

    db = sqlite3.connect(":memory:")
    _seed_documents(db, [("h1", "doc body " * 10, "docs/a.md")])

    fake_index = _FakeVecIndex(raise_on_save=RuntimeError("disk full"))
    deps = _build_run_embed_deps(open_usearch_index=lambda: fake_index)

    from kairix.core.embed.embed import run_embed

    with caplog.at_level(logging.ERROR):
        result = run_embed(db, batch_size=10, deps=deps)

    assert isinstance(result, dict)
    error_lines = [r.message for r in caplog.records if "usearch final save failed" in r.message]
    assert error_lines, "expected an error log line about the failed save"
    assert "disk full" in error_lines[0]


@pytest.mark.unit
def test_run_embed_records_failed_chunks_when_embed_batch_raises(caplog) -> None:
    """When embed_batch raises, the affected chunks are recorded as failed and a
    warning lists the affected paths. Drives the failed_chunks branch in
    run_embed (lines 545-548).
    """
    import logging

    db = sqlite3.connect(":memory:")
    _seed_documents(
        db,
        [
            ("h1", "alpha alpha " * 10, "docs/alpha.md"),
            ("h2", "beta beta " * 10, "docs/beta.md"),
        ],
    )

    def _always_raises(_texts, *_a, **_kw):
        raise OSError("transient API outage")

    deps = _build_run_embed_deps(embed_batch=_always_raises)

    from kairix.core.embed.embed import run_embed

    with caplog.at_level(logging.WARNING):
        result = run_embed(db, batch_size=10, deps=deps)

    # Both docs failed — embedded=0, failed > 0.
    assert result["embedded"] == 0
    assert result["failed"] >= 2

    failure_summaries = [r.message for r in caplog.records if "chunks failed" in r.message]
    assert failure_summaries, "expected a 'chunks failed' summary log line"
    summary = failure_summaries[0]
    # Both seeded paths must appear in the path-sample summary.
    assert "docs/alpha.md" in summary
    assert "docs/beta.md" in summary


@pytest.mark.unit
def test_run_embed_logs_chunk_date_populated_count_when_documents_have_dates(caplog) -> None:
    """When at least one chunk has chunk_date populated, run_embed logs the
    populated count — not the warning. Drives the chunk_date>0 branch
    (lines 558-563) and proves the date-extraction round-trips.
    """
    import logging

    # Document body whose YAML frontmatter carries a date — extract_chunk_date
    # picks this up and run_embed surfaces the count via the info log.
    body_with_date = "---\ndate: 2026-04-15\n---\n\n" + "content " * 30

    db = sqlite3.connect(":memory:")
    _seed_documents(db, [("h1", body_with_date, "docs/dated.md")])

    deps = _build_run_embed_deps()

    from kairix.core.embed.embed import run_embed

    with caplog.at_level(logging.INFO):
        result = run_embed(db, batch_size=10, deps=deps)

    assert result["embedded"] >= 1

    populated_lines = [r.message for r in caplog.records if "chunk_date populated for" in r.message]
    assert populated_lines, "expected a 'chunk_date populated for N/M chunks' info log"
    # Sabotage check: if extract_chunk_date were broken or the chunk_date assertion path
    # in run_embed were the warning branch, the populated message would not appear at all.
    assert "/" in populated_lines[0]  # "for N/M chunks" embeds a fraction


@pytest.mark.unit
def test_run_embed_warns_when_no_chunks_have_chunk_date(caplog) -> None:
    """When zero chunks have chunk_date, run_embed emits the temporal-boost-inert
    warning instead of the populated info line. Drives the chunk_date==0 branch.
    """
    import logging

    # No frontmatter, no date in the path — extract_chunk_date returns None.
    db = sqlite3.connect(":memory:")
    _seed_documents(db, [("h1", "plain body content " * 30, "docs/plain.md")])

    deps = _build_run_embed_deps()

    from kairix.core.embed.embed import run_embed

    with caplog.at_level(logging.WARNING):
        result = run_embed(db, batch_size=10, deps=deps)

    assert result["embedded"] >= 1
    inert_warnings = [r.message for r in caplog.records if "temporal boost (TMP-7B) will be inert" in r.message]
    assert inert_warnings, "expected the 0/N chunk_date warning"
