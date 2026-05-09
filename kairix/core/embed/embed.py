"""
Core embedding logic — fetches vectors from Azure OpenAI and writes to kairix's SQLite.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable, Generator
from typing import Any

from .date_extract import extract_chunk_date
from .deps import EmbedDependencies
from .schema import EMBED_VECTOR_DIMS, SchemaVersionError

logger = logging.getLogger(__name__)

# Azure OpenAI
DEFAULT_DEPLOYMENT = "text-embedding-3-large"
DEFAULT_DIMS = EMBED_VECTOR_DIMS
DEFAULT_BATCH_SIZE = 250  # Balanced: large enough for throughput, small enough to avoid Azure 429s
MAX_RETRIES = 6  # used by OpenAI SDK max_retries

# Chunking — mirrors kairix's CHUNK_SIZE_TOKENS / CHUNK_OVERLAP_TOKENS
CHUNK_SIZE_CHARS = 3600  # ~900 tokens at 4 chars/token
CHUNK_OVERLAP_CHARS = 200


# ── Encoding ──────────────────────────────────────────────────────────────────


def build_hash_seq(content_hash: str, seq: int) -> str:
    """Build the hash_seq key used by usearch index metadata."""
    return f"{content_hash}_{seq}"


# ── Chunking ──────────────────────────────────────────────────────────────────


def _find_break_point(text: str, pos: int, end: int, chunk_size: int) -> int:
    """Find the best break point within a chunk boundary.

    Prefers paragraph breaks, then sentence breaks, then falls back to the raw end.
    """
    if end >= len(text):
        return end

    half = pos + chunk_size // 2
    para_break = text.rfind("\n\n", pos, end)
    if para_break > half:
        return para_break + 2

    sent_break = max(text.rfind(". ", pos, end), text.rfind(".\n", pos, end))
    if sent_break > half:
        return sent_break + 1

    return end


def chunk_text(
    text: str, chunk_size: int = CHUNK_SIZE_CHARS, overlap: int = CHUNK_OVERLAP_CHARS
) -> list[dict[str, Any]]:
    """
    Split text into overlapping chunks. Returns list of {seq, pos, text}.
    Mirrors kairix's chunkDocument() logic for consistency.
    Tries to split on paragraph boundaries first, falls back to char splits.
    """
    if len(text) <= chunk_size:
        return [{"seq": 0, "pos": 0, "text": text}]

    chunks = []
    pos = 0
    seq = 0

    while pos < len(text):
        end = min(pos + chunk_size, len(text))
        end = _find_break_point(text, pos, end, chunk_size)

        chunk_text_val = text[pos:end].strip()
        if chunk_text_val:
            chunks.append({"seq": seq, "pos": pos, "text": chunk_text_val})
            seq += 1

        pos = end - overlap if end < len(text) else len(text)

    return chunks


# ── Azure API ─────────────────────────────────────────────────────────────────


def _get_azure_config() -> tuple[str, str, str]:  # pragma: no cover
    """
    Read embed API config via ``get_credentials("embed")``. Supports Azure,
    OpenRouter, or any OpenAI-compatible endpoint.

    Production lazy default for ``EmbedDependencies.get_azure_config``;
    tests inject a fake callable via ``run_embed(deps=...)`` so this
    function is never test-reachable. The credential resolution itself
    is exercised in ``tests/test_credentials.py``.

    Raises OSError when credentials cannot be resolved.
    """
    from kairix.credentials import Credentials, get_credentials

    creds = get_credentials("embed")
    if not isinstance(creds, Credentials):
        raise OSError("Embed credentials not available.")
    api_key = creds.api_key
    endpoint = creds.endpoint
    deployment = creds.model or DEFAULT_DEPLOYMENT

    if not api_key:
        raise OSError(
            "KAIRIX_LLM_API_KEY / KAIRIX_EMBED_API_KEY not set. "
            "Set the env var, add to secrets file, or configure Key Vault."
        )
    if not endpoint:
        raise OSError(
            "KAIRIX_LLM_ENDPOINT / KAIRIX_EMBED_ENDPOINT not set. "
            "Set the env var, add to secrets file, or configure Key Vault."
        )

    # Normalise endpoint — strip trailing slash, we'll add the path
    endpoint = endpoint.rstrip("/")
    return api_key, endpoint, deployment


def preflight_check(
    api_key: str,
    endpoint: str,
    deployment: str,
    *,
    client: Any | None = None,
) -> int:
    """
    Verify the embedding API is reachable with a single-item embed call.
    Returns embedding dimensions on success, raises on failure.
    Does NOT touch the DB — safe to call before any writes.

    ``client`` is an injection seam for tests — pass an OpenAI-compatible
    fake whose ``embeddings.create(...)`` returns a response with a
    ``.data[0].embedding`` list. Production callers leave it as ``None`` so
    the real client is built lazily via ``make_openai_client``.
    """
    if client is None:  # pragma: no cover
        from kairix.credentials import make_openai_client

        client = make_openai_client(api_key, endpoint, max_retries=2, timeout=30.0)
    response = client.embeddings.create(
        model=deployment,
        input=["preflight check"],
        dimensions=DEFAULT_DIMS,
    )
    dims = len(response.data[0].embedding)
    logger.info("Preflight OK — dims=%d", dims)
    return dims


# Reuse a single SDK client across all batches. Connection pooling and the
# SDK's internal rate-limiter state carry over between calls, which prevents
# redundant Retry-After waits when the server quota is actually available.
_embed_client = None
_embed_client_key: tuple[str, str] = ("", "")


def _get_embed_client(api_key: str, endpoint: str) -> Any:  # pragma: no cover
    """Return a cached OpenAI client. Creates a new one if credentials change.

    Production-only — every test that exercises ``embed_batch`` injects a
    ``client=`` kwarg, bypassing this cache. The cache is reachable only
    when the production lazy default fires (``client is None``).
    """
    from kairix.credentials import make_openai_client

    global _embed_client, _embed_client_key
    key = (api_key, endpoint)
    if _embed_client is not None and _embed_client_key == key:
        return _embed_client

    _embed_client = make_openai_client(api_key, endpoint, max_retries=MAX_RETRIES, timeout=60.0)
    _embed_client_key = key
    return _embed_client


def embed_batch(
    texts: list[str],
    api_key: str,
    endpoint: str,
    deployment: str,
    dims: int = DEFAULT_DIMS,
    *,
    client: Any | None = None,
) -> list[list[float]]:
    """
    Embed a batch of texts via Azure OpenAI using the OpenAI SDK.

    Client is reused across batches for connection pooling and rate-limiter
    state persistence. The SDK handles retry with exponential backoff and
    Retry-After headers automatically.

    ``client`` is an injection seam for tests — pass an OpenAI-compatible
    fake whose ``embeddings.create(...)`` returns a response with ``.data``
    items carrying ``.index`` and ``.embedding``. Production callers leave
    it as ``None`` so the cached production client is reused.

    Returns list of float vectors in same order as input texts.
    Raises on persistent failures after SDK retries are exhausted.
    On BadRequestError (batch too large), splits and recurses.
    """
    import openai

    if not texts:
        return []

    if client is None:  # pragma: no cover
        client = _get_embed_client(api_key, endpoint)

    try:
        response = client.embeddings.create(
            model=deployment,
            input=texts,
            dimensions=dims,
        )
        results = sorted(response.data, key=lambda x: x.index)
        return [list(r.embedding) for r in results]
    except openai.BadRequestError:
        if len(texts) == 1:
            raise
        mid = len(texts) // 2
        logger.warning(
            "BadRequestError on batch of %d — splitting into %d + %d",
            len(texts),
            mid,
            len(texts) - mid,
        )
        left = embed_batch(texts[:mid], api_key, endpoint, deployment, dims, client=client)
        right = embed_batch(texts[mid:], api_key, endpoint, deployment, dims, client=client)
        return left + right


# ── DB writes ─────────────────────────────────────────────────────────────────


def stage_embedding(
    db: sqlite3.Connection,
    content_hash: str,
    seq: int,
    pos: int,
    _vector: list[float],
    model: str,
    embedded_at: int,
    chunk_date: str | None = None,
) -> None:
    """
    Write chunk metadata to content_vectors.

    content_vectors is a normal SQLite table and supports INSERT OR REPLACE.
    Vectors are written to the usearch ANN index separately via
    _update_usearch_index() at batch commit time.

    _vector is accepted for call-site compatibility (callers pass it
    positionally) but is not used here — vectors are written to the
    usearch ANN index by the caller.

    chunk_date is the ISO date extracted from the document (frontmatter or path).
    It is the same for all chunks of a given document (document-level property).
    """
    db.execute(
        "INSERT OR REPLACE INTO content_vectors"
        " (hash, seq, pos, model, embedded_at, chunk_date) VALUES (?, ?, ?, ?, ?, ?)",
        (content_hash, seq, pos, model, embedded_at, chunk_date),
    )


# ── Batch generator ───────────────────────────────────────────────────────────


def batched(items: list[Any], size: int) -> Generator[list[Any], None, None]:
    """Yield successive batches of `size` from `items`."""
    for i in range(0, len(items), size):
        yield items[i : i + size]


# ── usearch index update ─────────────────────────────────────────────────────


def _open_usearch_index() -> Any:  # pragma: no cover
    """Open (or create) the usearch ANN index for the embed run.

    Production-only — every test that exercises ``run_embed`` injects an
    ``open_usearch_index`` callable via ``EmbedDependencies`` (typically a
    ``lambda: None`` or a ``_FakeVecIndex`` double). The real
    ``VectorIndex.load()`` requires a writable on-disk path and embedded
    vectors that match the current schema, neither of which is available
    in unit tests.
    """
    try:
        from kairix.core.search.vec_index import VectorIndex
        from kairix.paths import db_path as get_db_path

        db_p = get_db_path()
        index_path = db_p.parent / "vectors.usearch"
        meta_path = db_p.parent / "vectors.meta.json"
        idx = VectorIndex(index_path=index_path, meta_path=meta_path, db_path=db_p)
        idx.load()  # auto-deletes if dims mismatch
        return idx
    except Exception as e:
        logger.error("usearch index open failed: %s", e)
        return None


# ── Extracted helpers (run_embed decomposition) ─────────────────────────────


def _gather_pending_chunks(
    db: sqlite3.Connection,
    force: bool,
    doc_root: str | None,
) -> tuple[list[dict[str, Any]], int]:
    """Gather chunks that need embedding.

    In force mode, clears existing vectors and selects all documents.
    In incremental mode, selects only documents not yet embedded.

    Returns (all_chunks, document_count) where each chunk is a dict with
    keys: hash, seq, pos, text, path, chunk_date.
    """
    if force:
        logger.info("--force: clearing all existing vectors")
        db.execute("DELETE FROM content_vectors")
        db.commit()

    if force:
        rows = db.execute("""
            SELECT c.hash, c.doc, d.path
            FROM content c
            JOIN documents d ON c.hash = d.hash
            WHERE d.active = 1
              AND c.doc IS NOT NULL
              AND length(c.doc) > 0
        """).fetchall()
    else:
        rows = db.execute("""
            SELECT c.hash, c.doc, d.path
            FROM content c
            JOIN documents d ON c.hash = d.hash
            LEFT JOIN content_vectors v ON c.hash = v.hash AND v.seq = 0
            WHERE v.hash IS NULL
              AND d.active = 1
              AND c.doc IS NOT NULL
              AND length(c.doc) > 0
        """).fetchall()

    all_chunks: list[dict[str, Any]] = []
    for content_hash, body, path in rows:
        doc_date = extract_chunk_date(body, path, document_root=doc_root)
        for chunk in chunk_text(body):
            all_chunks.append(
                {
                    "hash": content_hash,
                    "seq": chunk["seq"],
                    "pos": chunk["pos"],
                    "text": chunk["text"],
                    "path": path,
                    "chunk_date": doc_date,
                }
            )

    return all_chunks, len(rows)


def _embed_and_store_batch(
    batch: list[dict[str, Any]],
    batch_idx: int,
    db: sqlite3.Connection,
    vec_index: Any,
    api_key: str,
    endpoint: str,
    deployment: str,
    dims: int,
    now: int,
    save_interval: int,
    embed_batch_fn: Callable[..., list[list[float]]] | None = None,
) -> tuple[int, list[dict[str, Any]]]:
    """Embed a single batch and write results to DB + usearch index.

    Returns (embedded_count, failed_chunks) for this batch.
    """
    _embed = embed_batch_fn or embed_batch
    texts = [c["text"] for c in batch]

    try:
        vectors = _embed(texts, api_key, endpoint, deployment, dims)
    except (RuntimeError, KeyError, ValueError, OSError) as e:
        logger.error(
            "Batch %d failed: %s — logging %d chunks as failed",
            batch_idx,
            e,
            len(batch),
        )
        return 0, list(batch)

    # Defend against a partial response — the backend may return fewer vectors
    # than texts (rate-limit, partial 5xx, mocked dev backends). Without this
    # guard, ``zip(strict=False)`` would silently truncate and we'd report all
    # chunks as embedded while staging only the matched ones — a silent
    # over-count surfaced by the partial-response contract test.
    matched = batch[: len(vectors)]
    unaccounted = list(batch[len(vectors) :])
    if unaccounted:
        logger.error(
            "Batch %d: backend returned %d vectors for %d texts — %d chunks unaccounted",
            batch_idx,
            len(vectors),
            len(batch),
            len(unaccounted),
        )

    try:
        with db:
            for chunk, vector in zip(matched, vectors, strict=True):
                stage_embedding(
                    db,
                    chunk["hash"],
                    chunk["seq"],
                    chunk["pos"],
                    vector,
                    deployment,
                    now,
                    chunk_date=chunk.get("chunk_date"),
                )
        if vec_index is not None:
            try:
                batch_hash_seqs = [build_hash_seq(c["hash"], c["seq"]) for c in matched]
                vec_index.add_vectors(batch_hash_seqs, vectors)
                if (batch_idx + 1) % save_interval == 0:
                    vec_index.save()
            except Exception as e:
                logger.error("usearch batch %d failed: %s", batch_idx, e)
        return len(matched), unaccounted
    except sqlite3.Error as e:
        logger.error("DB write for batch %d failed: %s", batch_idx, e)
        return 0, list(batch)


def _save_index_checkpoint(vec_index: Any) -> None:
    """Final save of the usearch ANN index to disk."""
    if vec_index is None:
        return
    try:
        vec_index.save()
        logger.info("usearch: saved index with %d vectors", len(vec_index))
    except Exception as e:
        logger.error("usearch final save failed: %s", e)


# ── Main embed runner ─────────────────────────────────────────────────────────


def run_embed(
    db: sqlite3.Connection,
    force: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
    limit: int | None = None,
    deps: EmbedDependencies | None = None,
) -> dict[str, Any]:
    """
    Main embedding loop. Reads pending chunks, calls Azure, writes vectors.

    Args:
        db:         Open SQLite connection (caller holds the lock)
        force:      Re-embed everything, not just pending
        batch_size: Chunks per Azure API call (Azure supports up to 2048; default 500)
        limit:      Cap total chunks (for validation/testing)
        deps:       Injectable dependencies. Defaults to production implementations.

    Returns dict with: embedded, skipped, failed, duration_s, estimated_cost_usd
    """
    if deps is None:  # pragma: no cover
        deps = EmbedDependencies()

    assert deps.get_document_root is not None
    assert deps.get_azure_config is not None
    assert deps.preflight_check is not None
    assert deps.migrate_content_vectors is not None
    assert deps.open_usearch_index is not None

    doc_root = deps.get_document_root()

    api_key, endpoint, deployment = deps.get_azure_config()

    actual_dims = deps.preflight_check(api_key, endpoint, deployment)
    if actual_dims != DEFAULT_DIMS:
        raise SchemaVersionError(
            f"Azure returned {actual_dims} dims but expected {DEFAULT_DIMS}. "
            f"Check KAIRIX_EMBED_MODEL and dimensions setting."
        )

    deps.migrate_content_vectors(db)

    all_chunks, doc_count = _gather_pending_chunks(db, force, doc_root)

    if limit:
        all_chunks = all_chunks[:limit]

    total = len(all_chunks)
    if total == 0:
        logger.info("Nothing to embed — index is up to date.")
        return {
            "embedded": 0,
            "skipped": 0,
            "failed": 0,
            "duration_s": 0,
            "estimated_cost_usd": 0.0,
        }

    logger.info(
        "Embedding %d chunks across %d documents (batch_size=%d)",
        total,
        doc_count,
        batch_size,
    )

    embedded = 0
    failed_chunks: list[dict[str, Any]] = []
    start_time = time.time()
    now = int(start_time)

    vec_index = deps.open_usearch_index()
    save_interval = 10

    for batch_idx, batch in enumerate(batched(all_chunks, batch_size)):
        batch_ok, batch_failed = _embed_and_store_batch(
            batch,
            batch_idx,
            db,
            vec_index,
            api_key,
            endpoint,
            deployment,
            actual_dims,
            now,
            save_interval,
            embed_batch_fn=deps.embed_batch,
        )
        embedded += batch_ok
        failed_chunks.extend(batch_failed)
        if batch_ok:
            logger.info(
                "Embed progress: %d/%d chunks (%.0f%%) — batch %d",
                embedded,
                total,
                100.0 * embedded / total if total > 0 else 0,
                batch_idx + 1,
            )

    _save_index_checkpoint(vec_index)

    duration_s = time.time() - start_time
    estimated_tokens = embedded * 200
    estimated_cost = (estimated_tokens / 1000) * 0.00013

    if failed_chunks:
        failed_paths = list({c["path"] for c in failed_chunks})[:10]
        sample = [str(p)[:200] for p in failed_paths]
        logger.warning("%d chunks failed. Affected paths (sample): %s", len(failed_chunks), sample)

    chunk_date_count = sum(1 for c in all_chunks if c.get("chunk_date"))
    if chunk_date_count == 0 and total > 0:
        logger.warning(
            "embed: 0/%d chunks have chunk_date — temporal boost (TMP-7B) will be inert. "
            "Ensure documents have a date in frontmatter (date: YYYY-MM-DD) or in their filename.",
            total,
        )
    else:
        logger.info(
            "embed: chunk_date populated for %d/%d chunks (%.1f%%)",
            chunk_date_count,
            total,
            100 * chunk_date_count / total,
        )

    return {
        "embedded": embedded,
        "skipped": total - embedded - len(failed_chunks),
        "failed": len(failed_chunks),
        "failed_paths": list({c["path"] for c in failed_chunks}),
        "duration_s": round(duration_s, 1),
        "estimated_cost_usd": round(estimated_cost, 4),
        "total_chunks": total,
        "chunk_date_count": chunk_date_count,
    }
