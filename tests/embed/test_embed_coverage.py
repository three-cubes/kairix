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
    from unittest.mock import MagicMock

    import openai

    err = openai.AuthenticationError(
        message="HTTP 401",
        response=MagicMock(status_code=401),
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
