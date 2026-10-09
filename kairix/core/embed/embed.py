"""
Core embedding logic — fetches vectors from Azure OpenAI and writes to kairix's SQLite.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Generator
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .date_extract import extract_chunk_date
from .deps import EmbedDependencies
from .embedding_cache import EmbeddingCache, hash_chunk_text
from .schema import EMBED_VECTOR_DIMS, SchemaVersionError

logger = logging.getLogger(__name__)

# Azure OpenAI
DEFAULT_DEPLOYMENT = "text-embedding-3-large"
DEFAULT_DIMS = EMBED_VECTOR_DIMS
DEFAULT_BATCH_SIZE = 250  # Balanced: large enough for throughput, small enough to avoid Azure 429s
MAX_RETRIES = 6  # used by OpenAI SDK max_retries

# Parallel-batches ceiling. Above this, Azure 429 / quota-burn risk dominates the
# throughput gain and the worker can saturate downstream search-side reads off
# the same SQLite. Operators wanting more should re-shape the corpus or split
# the catch-up across multiple worker hosts.
# See docs/operations/runbooks/worker-memory-and-swap.md for sizing guidance.
MAX_PARALLEL_BATCHES = 10
DEFAULT_PARALLEL_BATCHES = 1  # default-safe: today's serial behaviour

# GH #394 — per-batch WAL checkpoint cadence during embed catch-up.
# R3 (#389) checkpoints WAL every 10 minutes in the maintenance loop, but
# that tick can't fire while embed is mid-transaction. During a 678K
# backfill the WAL grew to 3.8 GB before R3 could reclaim. Issuing a
# PASSIVE checkpoint every Nth batch keeps the WAL bounded without
# blocking concurrent readers (TRUNCATE would block; PASSIVE returns
# the (busy, log, checkpointed) tuple without waiting). Tests override
# the cadence via the ``wal_checkpoint_every_n_batches`` kwarg so 5
# batches at cadence 2 fires twice deterministically. Set to 0 to
# disable (production: keep the 10-batch default; tests: set to 0 to
# isolate the no-checkpoint baseline).
DEFAULT_WAL_CHECKPOINT_EVERY_N_BATCHES = 10

# F17 — chunk_date appears as a dict key in both producer and consumer paths;
# extract so renames hit a single edit site.
_KEY_CHUNK_DATE = "chunk_date"

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


def get_azure_config_from_credentials() -> tuple[str, str, str]:  # pragma: no cover — lazy DI default
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
    if client is None:  # pragma: no cover  # prod lazy default; tests inject client=fake
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


def _get_embed_client(api_key: str, endpoint: str) -> Any:  # pragma: no cover  # prod-only module cache
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

    if client is None:  # pragma: no cover  # prod lazy default; tests inject client=fake
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


# ── 429 resilience (#475) ─────────────────────────────────────────────────────

# Bounded outer retry around the provider call. The OpenAI SDK already
# retries 429s internally (MAX_RETRIES above); this loop catches the
# RateLimitError that ESCAPES SDK retries — previously a raw traceback
# that killed the whole run mid-catch-up.
RATE_LIMIT_MAX_ATTEMPTS = 5
RATE_LIMIT_MAX_BACKOFF_S = 60.0

_RATE_LIMIT_EXHAUSTED_MSG = (
    "Embedding provider returned HTTP 429 on every attempt ({attempts} attempts). "
    "fix: your embedding deployment is rate-limited (HTTP 429). Raise its quota or re-run later. "
    "next: kairix embed picks up where it left off (cache-hit on completed chunks). "
    "run: kairix embed"
)


def retry_after_seconds(exc: Exception) -> float | None:
    """Extract the Retry-After header (seconds) from an SDK error, if present.

    Reads ``exc.response.headers["retry-after"]`` defensively — the
    openai SDK attaches the httpx response to ``RateLimitError``, but
    fakes and older SDKs may not. Returns ``None`` when the header is
    absent or unparseable so the caller falls back to exponential backoff.
    """
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def with_rate_limit_retry(
    embed_batch_fn: Callable[..., list[list[float]]],
    *,
    max_attempts: int = RATE_LIMIT_MAX_ATTEMPTS,
    sleep_fn: Callable[[float], None] = time.sleep,
    max_backoff_s: float = RATE_LIMIT_MAX_BACKOFF_S,
) -> Callable[..., list[list[float]]]:
    """Wrap an embed-batch callable with bounded 429 retry (#475).

    A ``RateLimitError`` that escapes the SDK's own retries is caught
    here; the wrapper waits per the response's Retry-After header (or
    exponential backoff capped at ``max_backoff_s``) and retries up to
    ``max_attempts`` total attempts. Exhaustion raises ``RuntimeError``
    with an F21-shaped remediation message — the batch loop catches
    RuntimeError, marks the batch's chunks failed, and the run continues
    instead of dying with a raw traceback.

    ``sleep_fn`` is the injectable sleeper seam (production:
    ``time.sleep`` via ``EmbedDependencies.rate_limit_sleep``); tests
    pass a recorder so backoff assertions pay zero wall-clock time.
    """

    def _embed_with_rate_limit_retry(*args: Any, **kwargs: Any) -> list[list[float]]:
        import openai

        attempt = 0
        while True:
            attempt += 1
            try:
                return embed_batch_fn(*args, **kwargs)
            except openai.RateLimitError as exc:
                if attempt >= max_attempts:
                    raise RuntimeError(_RATE_LIMIT_EXHAUSTED_MSG.format(attempts=max_attempts)) from exc
                delay = retry_after_seconds(exc)
                if delay is None:
                    delay = float(2**attempt)
                delay = min(delay, max_backoff_s)
                logger.warning(
                    "Embedding provider rate-limited (429) — attempt %d/%d, waiting %.1fs before retry",
                    attempt,
                    max_attempts,
                    delay,
                )
                sleep_fn(delay)

    return _embed_with_rate_limit_retry


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


def open_usearch_index_for_paths(
    *,
    index_path: Path,
    meta_path: Path,
    db_path: Path,
) -> Any:
    """Open (or create) the usearch ANN index at the supplied paths.

    Uses :meth:`VectorIndex.load_or_recreate` so a corrupt on-disk file
    auto-recovers to a fresh empty index instead of returning None
    (the silent-no-op bug seen 2026-05-31 — see
    tests/e2e/test_vec_index_corrupt_recovery_e2e.py).

    Always returns a usable VectorIndex. Callers don't have to handle
    None and don't have to inspect file state — the recovery happened
    here.
    """
    from kairix.core.search.vec_index import VectorIndex

    # GH #352 — worker is the only mutate-capable VectorIndex caller.
    # read_only=False (default, named explicitly here for clarity)
    # loads the index fully into memory at .load() time so the first
    # add_vectors() call has no first-write conversion path to hit.
    idx = VectorIndex(index_path=index_path, meta_path=meta_path, db_path=db_path, read_only=False)
    count, status = idx.load_or_recreate()
    logger.info(
        "vec_index: opened at %s (vectors=%d, status=%s)",
        index_path,
        count,
        status,
    )
    return idx


def open_default_usearch_index() -> Any:  # pragma: no cover  # prod lazy default; deps injection
    """Production default for ``EmbedDependencies.open_usearch_index``.

    Thin wrapper that resolves canonical paths from ``KairixPaths`` and
    delegates to :func:`open_usearch_index_for_paths`. Tests inject
    :func:`open_usearch_index_for_paths` directly with tmp_path-scoped
    paths so the same recovery logic is exercised under integration +
    E2E coverage (the prior version had ``# pragma: no cover`` on the
    whole body and the recovery path was untested — that's why the
    production silent-None bug shipped).

    Returns ``None`` (skipping the usearch open + per-batch add) only
    when ``worker_writes_vec_index()`` is False — see #335 for context.
    """
    from kairix.paths import worker_writes_vec_index

    if not worker_writes_vec_index():
        logger.info(
            "vec_index: worker write disabled (KAIRIX_WORKER_WRITES_VEC_INDEX!=1) — "
            "SQLite content_vectors continues to advance; usearch on-disk index will "
            "drift (see #335 for rebuild plan)."
        )
        return None
    from kairix.paths import db_path as get_db_path

    db_p = get_db_path()
    return open_usearch_index_for_paths(
        index_path=db_p.parent / "vectors.usearch",
        meta_path=db_p.parent / "vectors.meta.json",
        db_path=db_p,
    )


# ── Extracted helpers (run_embed decomposition) ─────────────────────────────


# Collection name the bundled reference library is scanned under (must
# match kairix.core.embed.use_cases.REFERENCE_LIBRARY_NAME — the scanner
# writes it into documents.collection).
REFERENCE_LIBRARY_COLLECTION = "reference-library"


def _apply_reflib_index_mode(
    rows: list[Any],
    reflib_index_mode: str,
) -> list[Any]:
    """Apply the ``reference_library.index`` mode to gathered rows (#475).

    Each row's last element is ``documents.collection`` (NULL on legacy
    fixtures without the column — treated as a user document).

    * ``skip`` — reference-library rows never embed.
    * ``lazy`` — reference-library rows embed only in a run with no
      pending user-document rows; otherwise they're deferred so the
      user's own documents finish first (the next run picks them up).
    * ``eager`` (default) — no filtering; the gather's ORDER BY already
      places user documents first within the run.
    """
    if reflib_index_mode not in ("skip", "lazy"):
        return rows
    user_rows = [r for r in rows if r[-1] != REFERENCE_LIBRARY_COLLECTION]
    deferred = len(rows) - len(user_rows)
    if deferred == 0:
        return rows
    if reflib_index_mode == "skip":
        logger.info(
            "reference-library: index mode 'skip' — %d bundled reference documents excluded from embedding",
            deferred,
        )
        return user_rows
    # lazy: defer only while user documents are still pending.
    if user_rows:
        logger.info(
            "reference-library: index mode 'lazy' — deferring %d reference documents until your own "
            "documents finish embedding (the next embed run picks them up)",
            deferred,
        )
        return user_rows
    return rows


def _dedupe_chunks_by_hash_seq(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse duplicate chunk identities before embedding.

    ``content_vectors`` is keyed by ``(hash, seq)`` and SQLite uses
    ``INSERT OR REPLACE`` at that identity. USEARCH has no matching
    upsert/delete path, so feeding duplicate ``hash_seq`` values inflates the
    ANN index while SQLite collapses to one row. Duplicate active document rows
    with the same content hash are the known producer.
    """
    seen: set[tuple[str, int]] = set()
    deduped: list[dict[str, Any]] = []
    for chunk in chunks:
        key = (str(chunk["hash"]), int(chunk["seq"]))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(chunk)
    skipped = len(chunks) - len(deduped)
    if skipped:
        logger.info(
            "embed: skipped %d duplicate chunk identity row(s) before vector writes",
            skipped,
        )
    return deduped


def _gather_pending_chunks(
    db: sqlite3.Connection,
    force: bool,
    doc_root: str | None,
    reflib_index_mode: str = "eager",
) -> tuple[list[dict[str, Any]], int]:
    """Gather chunks that need embedding.

    In force mode, clears existing vectors and selects all documents.
    In incremental mode, selects only documents not yet embedded.

    User-docs-first (#475): rows are ordered so reference-library
    documents come after every other collection — the operator's own
    documents embed (and become vector-searchable) first.
    ``reflib_index_mode`` further filters reference-library rows per
    the ``reference_library.index`` config (see
    :func:`_apply_reflib_index_mode`).

    Returns (all_chunks, document_count) where each chunk is a dict with
    keys: hash, seq, pos, text, path, chunk_date.
    """
    if force:
        logger.info("--force: clearing all existing vectors")
        db.execute("DELETE FROM content_vectors")
        db.commit()

    # GH #329 — pull documents.source_modified_at so we can fall back to
    # connector-supplied envelope timestamps (SharePoint lastModifiedDateTime,
    # GitHub commit date, etc.) when the body-text extractor can't find a
    # frontmatter / path-derived date. Closes the 98% temporal-boost coverage
    # gap reported by the chunk_date_populated onboard check. Older test
    # fixtures use a minimal documents schema without this column — the
    # PRAGMA guard keeps them working while production gets the fallback.
    # F63-bounded: PRAGMA table_info returns one row per column (schema-bounded, ≤O(20)).
    _doc_cols = {row[1] for row in db.execute("PRAGMA table_info(documents)").fetchall()}
    smt_select = "d.source_modified_at" if "source_modified_at" in _doc_cols else "NULL AS source_modified_at"
    # #475 — documents.collection drives user-docs-first ordering and the
    # reference_library.index mode. Same PRAGMA guard as above: legacy
    # fixtures without the column behave as all-user-documents.
    has_collection = "collection" in _doc_cols
    col_select = "d.collection" if has_collection else "NULL AS collection"
    order_by = (
        f"ORDER BY CASE WHEN d.collection = '{REFERENCE_LIBRARY_COLLECTION}' THEN 1 ELSE 0 END, d.path"
        if has_collection
        else "ORDER BY d.path"
    )

    if force:
        # F63-bounded: hourly embed worker tick consumes ALL candidates per cycle by design;
        # the per-batch chunking (`batch_size` upstream) is what bounds memory pressure, not row count.
        # Scale risk flagged for future streaming-cursor refactor — see #211 for context.
        rows = db.execute(f"""
            SELECT c.hash, c.doc, d.path, {smt_select}, {col_select}
            FROM content c
            JOIN documents d ON c.hash = d.hash
            WHERE d.active = 1
              AND c.doc IS NOT NULL
              AND length(c.doc) > 0
            {order_by}
        """).fetchall()
    else:
        # "needs embedding" = no *embedded* vector for this content hash. We join
        # on ``v.model IS NOT NULL`` (every real embed writes ``model``; the
        # _SqliteChunkWriter placeholder writes only (hash, seq, pos)) instead of
        # ``v.seq = 0``. The placeholder lands at (hash, seq=enumerate-index): for
        # a doc's first chunk that is (hash, seq=0), so a ``seq = 0`` join treated
        # it as already-embedded and silently skipped chunk-0 of every multi-chunk
        # connector doc from vector embedding (BM25/FTS masked the gap).
        #
        # F63-bounded: hourly embed worker tick consumes candidates needing vectors; cycle gates further work.
        # Scale risk flagged for future streaming-cursor refactor — see #211 for context.
        rows = db.execute(f"""
            SELECT c.hash, c.doc, d.path, {smt_select}, {col_select}
            FROM content c
            JOIN documents d ON c.hash = d.hash
            LEFT JOIN content_vectors v ON c.hash = v.hash AND v.model IS NOT NULL
            WHERE v.hash IS NULL
              AND d.active = 1
              AND c.doc IS NOT NULL
              AND length(c.doc) > 0
            {order_by}
        """).fetchall()

    rows = _apply_reflib_index_mode(rows, reflib_index_mode)

    all_chunks: list[dict[str, Any]] = []
    for content_hash, body, path, source_modified_at, _collection in rows:
        # Body-text extractor first (matches Obsidian frontmatter / path
        # patterns); fall back to connector envelope timestamp (#329).
        doc_date = extract_chunk_date(body, path, document_root=doc_root) or source_modified_at
        for chunk in chunk_text(body):
            all_chunks.append(
                {
                    "hash": content_hash,
                    "seq": chunk["seq"],
                    "pos": chunk["pos"],
                    "text": chunk["text"],
                    "path": path,
                    _KEY_CHUNK_DATE: doc_date,
                }
            )

    return _dedupe_chunks_by_hash_seq(all_chunks), len(rows)


def _split_batch_against_cache(
    batch: list[dict[str, Any]],
    cache: EmbeddingCache | None,
    deployment: str,
    dims: int,
) -> tuple[
    list[tuple[dict[str, Any], list[float]]],
    list[dict[str, Any]],
]:
    """Split a batch into ``(cache_hits, cache_misses)``.

    Returns ``(hits, misses)`` where ``hits`` is a list of
    ``(chunk, vector_as_list_of_floats)`` pairs already populated from
    the persistent cache, and ``misses`` is the subset of ``batch`` that
    must go to the embed provider. With ``cache=None`` every chunk is a
    miss.

    Each chunk's ``text_hash`` is computed once here and stashed on the
    chunk dict so subsequent cache writes reuse the same hash without
    re-hashing.
    """
    if cache is None:
        for chunk in batch:
            chunk.setdefault("text_hash", hash_chunk_text(chunk["text"]))
        return [], list(batch)

    hashes: list[str] = []
    for chunk in batch:
        text_hash = chunk.setdefault("text_hash", hash_chunk_text(chunk["text"]))
        hashes.append(text_hash)

    cached = cache.get_many(deployment, dims, hashes)
    hits: list[tuple[dict[str, Any], list[float]]] = []
    misses: list[dict[str, Any]] = []
    for chunk in batch:
        cached_vec = cached.get(chunk["text_hash"])
        if cached_vec is not None:
            hits.append((chunk, cached_vec.tolist()))
        else:
            misses.append(chunk)
    return hits, misses


def _embed_batch_only(
    batch: list[dict[str, Any]],
    batch_idx: int,
    api_key: str,
    endpoint: str,
    deployment: str,
    dims: int,
    embed_batch_fn: Callable[..., list[list[float]]],
    *,
    cache: EmbeddingCache | None = None,
) -> tuple[list[dict[str, Any]], list[list[float]], list[dict[str, Any]], bool]:
    """Embed a single batch via the provider call only — no DB / index writes.

    Returns ``(matched_chunks, vectors, unaccounted_chunks, provider_failed)``.

    Cache-first: any chunk whose ``(deployment, dims, sha256(text))``
    tuple is present in the persistent cache short-circuits the provider
    call. The provider only sees the cache misses. After the provider
    response the new vectors are written to the cache BEFORE the caller
    persists them anywhere else — a crash between provider response and
    SQLite write leaves the cache with the vectors so the next run finds
    them and skips the provider.

    Split from the persistence step so multiple provider calls can run in
    parallel threads while the SQLite + usearch writes stay serialised
    on a single writer thread.

    ``provider_failed=True`` means the provider call raised — caller marks
    every miss in the batch as failed (cache hits are kept). ``unaccounted``
    are chunks the backend returned no vector for (partial 5xx / rate-limit).
    """
    hits, misses = _split_batch_against_cache(batch, cache, deployment, dims)

    miss_vectors: list[list[float]] = []
    provider_failed = False
    if misses:
        texts = [c["text"] for c in misses]
        try:
            miss_vectors = embed_batch_fn(texts, api_key, endpoint, deployment, dims)
        except (RuntimeError, KeyError, ValueError, OSError):
            logger.exception(
                "Batch %d failed — logging %d chunks as failed",
                batch_idx,
                len(misses),
            )
            provider_failed = True

    if provider_failed:
        # Cache hits are still good to surface — they cost nothing and
        # the production goal is to skip provider calls on chunks we
        # already paid for. Only the misses count as failed here.
        hit_chunks = [chunk for chunk, _ in hits]
        hit_vectors = [vec for _, vec in hits]
        return hit_chunks, hit_vectors, [], True

    matched_misses = misses[: len(miss_vectors)]
    unaccounted = list(misses[len(miss_vectors) :])
    if unaccounted:
        logger.error(
            "Batch %d: backend returned %d vectors for %d texts — %d chunks unaccounted",
            batch_idx,
            len(miss_vectors),
            len(misses),
            len(unaccounted),
        )

    if cache is not None and matched_misses:
        try:
            cache.put_many(
                deployment,
                dims,
                ((c["text_hash"], v) for c, v in zip(matched_misses, miss_vectors, strict=True)),
            )
        except sqlite3.Error:
            logger.exception("embedding_cache: put_many for batch %d failed (continuing)", batch_idx)

    matched = [chunk for chunk, _ in hits] + matched_misses
    vectors = [vec for _, vec in hits] + list(miss_vectors)
    if hits:
        logger.info(
            "Batch %d: %d/%d cache hits (saved %d provider calls)",
            batch_idx,
            len(hits),
            len(batch),
            len(hits),
        )
    return matched, vectors, unaccounted, False


def _embed_and_store_batch(
    batch: list[dict[str, Any]],
    batch_idx: int,
    db: sqlite3.Connection,
    vec_writer: Any,
    api_key: str,
    endpoint: str,
    deployment: str,
    dims: int,
    now: int,
    embed_batch_fn: Callable[..., list[list[float]]],
    *,
    cache: EmbeddingCache | None = None,
) -> tuple[int, list[dict[str, Any]]]:
    """Embed a single batch and write results to DB + usearch index.

    Returns (embedded_count, failed_chunks) for this batch.

    ``embed_batch_fn`` is the embedding callable — production passes the
    Azure-backed ``embed_batch``; tests pass a fake. No default — caller
    constructs the right callable at the boundary (the ``run_embed``
    function below threads ``deps.embed_batch`` through). Removing the
    legacy ``= None`` default closes the F6 test-seam violation.

    ``vec_writer`` is the :class:`VecIndexBatchWriter` (or test fake)
    that owns the vec_index lifecycle for the entire run; this helper
    just calls ``add_batch`` and the writer handles the per-N-batch
    incremental save cadence itself (#375).

    Kept for the ``--parallel 1`` (serial) path and for tests that
    exercise the per-batch boundary directly.
    """
    matched, vectors, unaccounted, azure_failed = _embed_batch_only(
        batch, batch_idx, api_key, endpoint, deployment, dims, embed_batch_fn, cache=cache
    )
    if azure_failed:
        # Cache hits already in `matched` are real and should still
        # persist. The miss-set is the failed set.
        failed = [c for c in batch if c not in matched]
    else:
        failed = list(unaccounted)

    if not matched:
        return 0, failed

    try:
        _stage_batch_embeddings(db, matched, vectors, deployment, now)
    except sqlite3.Error:
        logger.exception("DB write for batch %d failed", batch_idx)
        return 0, list(batch)

    vec_writer.add_batch(matched, vectors, batch_idx)
    return len(matched), failed


def _stage_batch_embeddings(
    db: sqlite3.Connection,
    matched: list[dict[str, Any]],
    vectors: list[list[float]],
    deployment: str,
    now: int,
) -> None:
    """Stage all matched chunk embeddings in a single SQLite transaction.

    Extracted from ``_embed_and_store_batch`` to keep that function under
    F16's cognitive-complexity ceiling — the nested ``with db:`` + per-chunk
    ``stage_embedding`` loop + per-vector index-write block nested above 15.
    """
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
                chunk_date=chunk.get(_KEY_CHUNK_DATE),
            )


# GH #375 — default cadence for incremental saves. The previous inline
# constant inside ``run_embed`` (``save_interval = 10``) is now the
# default for the ``VecIndexBatchWriter.save_every_n_batches`` kwarg so
# operators (and tests) can tune it without an embed.py edit. With
# DEFAULT_BATCH_SIZE=250 the default is one full ``vec_index.save()`` per
# 2 500 successfully-staged chunks — bounded write-amplification at
# production scale, the dominant cost on a 10 GB+ on-disk graph (see
# tests/core/embed/test_embed_vec_index_handle_lifecycle.py for the
# lifecycle contract this constant participates in).
DEFAULT_VEC_INDEX_SAVE_EVERY_N_BATCHES = 10


class VecIndexBatchWriter:
    """Once-per-run wrapper that owns the vec_index lifecycle.

    GH #375 — the previous shape opened ``vec_index`` once in
    :func:`run_embed` and fired ``save()`` every ``save_interval``
    batches via :func:`_add_batch_to_vec_index`. Correct, but the
    once-per-run invariant lived "by accident of where the call sat":
    a future refactor that moves the open into the batch loop would
    pass tests (none assert the lifecycle) while degrading throughput
    50x at production scale. This class makes the invariant
    contract-enforced:

      * ``__enter__`` is the single load point — opens / reuses the
        ``vec_index`` handle passed in, snapshots the inbound batch
        count, returns ``self``.
      * ``add_batch(matched, vectors, batch_idx)`` appends vectors and,
        every ``save_every_n_batches`` calls, fires an incremental
        ``vec_index.save()``. Failures are logged + swallowed —
        usearch errors must never break the SQLite write path.
      * ``__exit__`` fires a final ``vec_index.save()`` unconditionally
        when at least one batch was added, so the final partial window
        is durable. No-op when no batches were added (preserves the
        symmetry: zero-batch run → zero saves, one enter + one exit).

    Tolerates ``vec_index=None`` — the worker may run with the
    ``worker_writes_vec_index`` feature off (#335), in which case
    every method short-circuits. ``enter``/``exit`` still fire so the
    lifecycle counter assertions in the test suite stay symmetrical
    regardless of writer presence.

    Constructor seam: ``save_every_n_batches`` is the only knob, so
    tests inject a small value (2 or 3) and assert the save count
    directly; operators inject a larger value when paying down the
    per-save 10 GB-write cost on a real corpus. No env-var read, no
    test-only kwarg — pure F1 / F2 / F6 clean.
    """

    def __init__(
        self,
        vec_index: Any,
        save_every_n_batches: int = DEFAULT_VEC_INDEX_SAVE_EVERY_N_BATCHES,
    ) -> None:
        if save_every_n_batches < 1:
            raise ValueError(
                f"save_every_n_batches={save_every_n_batches} out of range [1..]. "
                "fix: pass save_every_n_batches >= 1 — values <1 disable incremental "
                "saves entirely, which loses bounded write-amplification on crash. "
                "next: pick 10 (default — balanced) or higher for paydown on a "
                "10 GB-plus on-disk index. "
                "run: see kairix.core.embed.embed.VecIndexBatchWriter for the contract."
            )
        self._vec_index = vec_index
        self._save_every_n_batches = save_every_n_batches
        self._batches_added = 0

    def __enter__(self) -> VecIndexBatchWriter:
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        # Context-manager protocol requires three positional params;
        # this writer doesn't care about exception propagation (we
        # never suppress) so all three are ``_``-prefixed (F19).
        #
        # Final save fires unconditionally when at least one batch was
        # added so the last partial window is durable. Zero batches
        # added → zero saves (matches the test_save_count_zero_when_no_batches_processed
        # symmetry assertion).
        if self._vec_index is None or self._batches_added == 0:
            return
        try:
            self._vec_index.save()
            logger.info("usearch: saved index with %d vectors", len(self._vec_index))
        except Exception as e:
            logger.exception("usearch final save failed: %s", e)

    def add_batch(
        self,
        matched: list[dict[str, Any]],
        vectors: list[list[float]],
        batch_idx: int,
    ) -> None:
        """Append one batch's vectors and, if the cadence threshold is hit,
        fire an incremental ``vec_index.save()``.

        ``batch_idx`` is the embed-loop's zero-based index, used only for
        log lines (the save cadence is governed by the writer's own
        internal counter so callers can't accidentally skip a save by
        passing a non-monotonic batch_idx).
        """
        if self._vec_index is None:
            return
        try:
            batch_hash_seqs = [build_hash_seq(c["hash"], c["seq"]) for c in matched]
            self._vec_index.add_vectors(batch_hash_seqs, vectors)
            self._batches_added += 1
            if self._batches_added % self._save_every_n_batches == 0:
                self._vec_index.save()
        except Exception:
            logger.exception("usearch batch %d failed", batch_idx)

    @property
    def batches_added(self) -> int:
        """Number of successful ``add_batch`` calls so far this run."""
        return self._batches_added


def _maybe_wal_checkpoint(db: sqlite3.Connection, batch_idx: int, every_n: int) -> None:
    """Issue ``PRAGMA wal_checkpoint(PASSIVE)`` every Nth batch (GH #394).

    Fires when ``(batch_idx + 1) % every_n == 0`` so batches=5, every=2
    triggers after the 2nd and 4th batches. ``every_n <= 0`` disables.

    PASSIVE (not TRUNCATE) — TRUNCATE blocks readers while it shrinks
    the WAL file; PASSIVE returns the ``(busy, log, checkpointed)``
    tuple without waiting and is safe to call mid-catch-up with
    concurrent search-side readers on the same SQLite.

    Swallows ``OperationalError`` (DB locked / busy) so the embed loop
    never aborts on a checkpoint failure — the next call retries, and
    R3's maintenance tick reclaims what we miss.
    """
    if every_n <= 0:
        return
    if (batch_idx + 1) % every_n != 0:
        return
    try:
        row = db.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
    except sqlite3.OperationalError:
        logger.warning("wal_checkpoint: PASSIVE failed at batch %d (busy?)", batch_idx + 1)
        return
    if row:
        busy, log_pages, checkpointed = int(row[0]), int(row[1]), int(row[2])
        logger.info(
            "wal_checkpoint: batch=%d busy=%d log_pages=%d checkpointed=%d",
            batch_idx + 1,
            busy,
            log_pages,
            checkpointed,
        )


# ── Parallel orchestration ───────────────────────────────────────────────────


def _validate_parallel(parallel: int) -> None:
    """Reject out-of-range ``--parallel`` values with an F21-shaped affordance.

    Default is 1 (serial — today's behaviour, no-op for operators who
    don't opt in). Range 1..MAX_PARALLEL_BATCHES. Above that the Azure
    quota-burn / 429-rate risk dominates the throughput gain — see the
    runbook.
    """
    if parallel < 1 or parallel > MAX_PARALLEL_BATCHES:
        raise ValueError(
            f"--parallel {parallel} out of range [1..{MAX_PARALLEL_BATCHES}]. "
            "fix: pass --parallel between 1 (serial, default) and "
            f"{MAX_PARALLEL_BATCHES} — higher values risk Azure rate-limit (429) "
            "burn and saturate downstream search-side reads on the same SQLite. "
            "next: pick --parallel 3 for the default VM size, --parallel 5 for "
            "a catch-up cycle on a worker sized per the runbook. "
            "run: see docs/operations/runbooks/worker-memory-and-swap.md "
            "for sizing guidance per corpus + per VM."
        )


def _persist_batch_result(
    matched: list[dict[str, Any]],
    vectors: list[list[float]],
    batch_idx: int,
    db: sqlite3.Connection,
    vec_writer: Any,
    deployment: str,
    now: int,
    db_lock: threading.Lock,
) -> bool:
    """Persist one Azure-completed batch under the single-writer lock.

    Returns True on success, False if the SQLite write raised (caller
    surfaces the batch's chunks as failed).

    The lock guards both SQLite (no shared cursor across threads) and
    the ``vec_writer.add_batch`` call (single-threaded today). Running
    this body under the lock keeps the writer fast — only the Azure
    call runs outside it, which is exactly where parallelism pays.
    """
    with db_lock:
        try:
            _stage_batch_embeddings(db, matched, vectors, deployment, now)
        except sqlite3.Error:
            logger.exception("DB write for batch %d failed", batch_idx)
            return False
        vec_writer.add_batch(matched, vectors, batch_idx)
        return True


# S107 waiver — 13 params vs ceiling 13 (post-#375 ``save_interval``
# fold into ``vec_writer``). Each is a distinct concern (chunks /
# batch_size / parallel / db handle / vec_writer / 4 Azure-config
# strings / current time / total count / embed_batch fn / optional
# cache); grouping any subset into a dataclass would obscure the
# contract more than it clarifies. Paydown: extract
# AzureEmbedSpec(api_key, endpoint, deployment, actual_dims) when the
# next signature change lands so this drops from 13 to 10.
def _run_embed_loop_parallel(
    all_chunks: list[dict[str, Any]],
    batch_size: int,
    parallel: int,
    db: sqlite3.Connection,
    vec_writer: Any,
    api_key: str,
    endpoint: str,
    deployment: str,
    actual_dims: int,
    now: int,
    total: int,
    embed_batch_fn: Callable[..., list[list[float]]],
    *,
    cache: EmbeddingCache | None = None,
    wal_checkpoint_every_n_batches: int = DEFAULT_WAL_CHECKPOINT_EVERY_N_BATCHES,
) -> tuple[int, list[dict[str, Any]]]:
    """Run the embed loop with up to ``parallel`` concurrent Azure calls.

    The Azure call runs on a ``ThreadPoolExecutor`` (releases the GIL
    during the network wait). The SQLite + usearch writes run under a
    single ``db_lock`` so each batch's persistence is serialised even
    when many embed futures complete in parallel. This keeps the
    SQLite connection single-threaded (no ``check_same_thread`` games)
    and the usearch index single-writer (matches today's contract).

    ``vec_writer`` is held open by the caller across the entire run
    (the once-per-run lifecycle locked in by GH #375). Persistence
    flows ``vec_writer.add_batch(...)`` under the lock; the writer
    itself owns the per-N-batches save cadence.

    F66-exempt: parallel batches are bounded by --parallel (1..10), not
    by per_tick_max_items — the bound is explicit at the CLI surface.

    Returns ``(embedded_count, failed_chunks)``.
    """
    embedded = 0
    failed_chunks: list[dict[str, Any]] = []
    db_lock = threading.Lock()
    batches_persisted = 0

    batches = list(enumerate(batched(all_chunks, batch_size)))

    with ThreadPoolExecutor(max_workers=parallel, thread_name_prefix="embed-batch") as pool:
        future_to_batch: dict[Future[Any], tuple[int, list[dict[str, Any]]]] = {
            pool.submit(
                _embed_batch_only,
                batch,
                batch_idx,
                api_key,
                endpoint,
                deployment,
                actual_dims,
                embed_batch_fn,
                cache=cache,
            ): (batch_idx, batch)
            for batch_idx, batch in batches
        }

        for fut in as_completed(future_to_batch):
            batch_idx, batch = future_to_batch[fut]
            matched, vectors, unaccounted, azure_failed = fut.result()
            if azure_failed:
                # Cache hits already in `matched` still persist; only the
                # provider-bound subset counts as failed.
                misses_failed = [c for c in batch if c not in matched]
                failed_chunks.extend(misses_failed)
                if not matched:
                    continue
            persisted = _persist_batch_result(
                matched,
                vectors,
                batch_idx,
                db,
                vec_writer,
                deployment,
                now,
                db_lock,
            )
            if not persisted:
                failed_chunks.extend(batch)
                continue
            failed_chunks.extend(unaccounted)
            embedded += len(matched)
            batches_persisted += 1
            with db_lock:
                _maybe_wal_checkpoint(db, batches_persisted - 1, wal_checkpoint_every_n_batches)
            if matched:
                logger.info(
                    "Embed progress: %d/%d chunks (%.0f%%) — batch %d",
                    embedded,
                    total,
                    100.0 * embedded / total if total > 0 else 0,
                    batch_idx + 1,
                )

    return embedded, failed_chunks


# ── Main embed runner ─────────────────────────────────────────────────────────


def _maybe_clear_vec_index_for_force(force: bool, vec_index: Any) -> None:
    """GH #352 — under ``--force`` we already cleared SQLite content_vectors
    in :func:`_gather_pending_chunks`. The on-disk usearch index must be
    cleared too, otherwise the first ``add_vectors()`` call previously
    triggered a convert-on-mutate path that loaded every existing vector
    into memory just to discard it. ``clear()`` drops the on-disk files
    and resets in-memory state, so the upcoming ``add_vectors`` calls
    build a fresh mutable index from empty. Lifted out of ``run_embed``
    to keep that function under the F16 cognitive-complexity ceiling.
    """
    if force and vec_index is not None and hasattr(vec_index, "clear"):
        logger.info("--force: clearing on-disk usearch index")
        vec_index.clear()


def _run_embed_loop_serial(
    all_chunks: list[dict[str, Any]],
    batch_size: int,
    db: sqlite3.Connection,
    vec_writer: Any,
    api_key: str,
    endpoint: str,
    deployment: str,
    actual_dims: int,
    now: int,
    total: int,
    embed_batch_fn: Callable[..., list[list[float]]],
    *,
    cache: EmbeddingCache | None = None,
    wal_checkpoint_every_n_batches: int = DEFAULT_WAL_CHECKPOINT_EVERY_N_BATCHES,
) -> tuple[int, list[dict[str, Any]]]:
    """Run the embed loop serially — today's default, ``--parallel 1``.

    ``vec_writer`` is the :class:`VecIndexBatchWriter` opened once by
    ``run_embed`` and kept open across every batch (GH #375). The loop
    just calls ``add_batch``; the writer handles the per-N-batches
    save cadence.

    Lifted out of ``run_embed`` for symmetry with
    ``_run_embed_loop_parallel`` and to keep ``run_embed`` under the F16
    cognitive-complexity ceiling once the parallel branch is added.

    ``wal_checkpoint_every_n_batches`` controls the per-batch WAL
    checkpoint cadence (GH #394). 0 disables.
    """
    embedded = 0
    failed_chunks: list[dict[str, Any]] = []
    for batch_idx, batch in enumerate(batched(all_chunks, batch_size)):
        batch_ok, batch_failed = _embed_and_store_batch(
            batch,
            batch_idx,
            db,
            vec_writer,
            api_key,
            endpoint,
            deployment,
            actual_dims,
            now,
            embed_batch_fn=embed_batch_fn,
            cache=cache,
        )
        embedded += batch_ok
        failed_chunks.extend(batch_failed)
        _maybe_wal_checkpoint(db, batch_idx, wal_checkpoint_every_n_batches)
        if batch_ok:
            logger.info(
                "Embed progress: %d/%d chunks (%.0f%%) — batch %d",
                embedded,
                total,
                100.0 * embedded / total if total > 0 else 0,
                batch_idx + 1,
            )
    return embedded, failed_chunks


def _log_embed_outcome(all_chunks: list[dict[str, Any]], failed_chunks: list[dict[str, Any]], total: int) -> int:
    """Log the post-run failure sample + chunk_date coverage; return the
    number of chunks carrying a ``chunk_date`` (``total`` must be > 0)."""
    if failed_chunks:
        failed_paths = list({c["path"] for c in failed_chunks})[:10]
        sample = [str(p)[:200] for p in failed_paths]
        logger.warning("%d chunks failed. Affected paths (sample): %s", len(failed_chunks), sample)

    chunk_date_count = sum(1 for c in all_chunks if c.get(_KEY_CHUNK_DATE))
    # ``total > 0`` is the caller's precondition (run_embed returns early on
    # an empty pending set), so zero dated chunks alone selects the warning.
    if chunk_date_count == 0:
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
    return chunk_date_count


def run_embed(
    db: sqlite3.Connection,
    force: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
    limit: int | None = None,
    deps: EmbedDependencies | None = None,
    parallel: int = DEFAULT_PARALLEL_BATCHES,
    save_every_n_batches: int = DEFAULT_VEC_INDEX_SAVE_EVERY_N_BATCHES,
    vec_writer: Any | None = None,
    wal_checkpoint_every_n_batches: int = DEFAULT_WAL_CHECKPOINT_EVERY_N_BATCHES,
) -> dict[str, Any]:
    """
    Main embedding loop. Reads pending chunks, calls Azure, writes vectors.

    Args:
        db:                              Open SQLite connection (caller holds the lock)
        force:                           Re-embed everything, not just pending
        batch_size:                      Chunks per Azure API call (Azure supports up to 2048; default 250)
        limit:                           Cap total chunks (for validation/testing)
        deps:                            Injectable dependencies. Defaults to production implementations.
        parallel:                        Number of batches to embed concurrently (1..10). Default 1
                                         is today's serial behaviour. Higher values run the Azure
                                         call on a ThreadPoolExecutor while the SQLite + usearch
                                         writes stay serialised under a single-writer lock. See
                                         docs/operations/runbooks/worker-memory-and-swap.md for
                                         sizing guidance.
        save_every_n_batches:            Per-N-batches incremental save cadence for the
                                         vec_index. Default 10 (with batch_size=250 → one
                                         full ``vec_index.save()`` per 2 500 staged
                                         chunks). Higher values reduce write-amplification
                                         on a 10 GB-plus on-disk index — see #375.
                                         Ignored when ``vec_writer`` is supplied (the
                                         caller-supplied writer owns its cadence).
        vec_writer:                      Optional pre-constructed :class:`VecIndexBatchWriter`
                                         (or test-fake context manager) — production
                                         callers leave this None so the writer is built
                                         from ``deps.open_usearch_index()`` here. Test
                                         callers pass a fake to assert on the
                                         once-per-run lifecycle (GH #375).
        wal_checkpoint_every_n_batches:  Issue ``PRAGMA wal_checkpoint(PASSIVE)`` after every
                                         Nth committed batch (GH #394). Defaults to 10. Bounds
                                         WAL growth during long catch-ups (R3's 10-minute
                                         maintenance tick can't fire mid-embed-transaction).
                                         0 disables; tests inject smaller values for
                                         deterministic assertions.

    Returns dict with: embedded, skipped, failed, duration_s, estimated_cost_usd
    """
    _validate_parallel(parallel)

    if deps is None:  # pragma: no cover  # prod lazy default; tests pass deps=
        deps = EmbedDependencies()

    doc_root = deps.get_document_root()

    api_key, endpoint, deployment = deps.get_azure_config()

    actual_dims = deps.preflight_check(api_key, endpoint, deployment)
    if actual_dims != DEFAULT_DIMS:
        raise SchemaVersionError(
            f"Azure returned {actual_dims} dims but expected {DEFAULT_DIMS}. "
            f"Check KAIRIX_EMBED_MODEL and dimensions setting."
        )

    deps.migrate_content_vectors(db)

    # #475 — reference_library.index mode (eager | lazy | skip) gates how
    # the bundled reference library participates in this run's gather.
    reflib_index_mode = deps.get_reflib_index_mode()

    all_chunks, doc_count = _gather_pending_chunks(db, force, doc_root, reflib_index_mode=reflib_index_mode)

    if limit:
        all_chunks = all_chunks[:limit]

    total = len(all_chunks)

    # GH #375 — open the vec_index handle ONCE per run and wrap it in a
    # :class:`VecIndexBatchWriter` whose ``__enter__`` / ``__exit__`` mark
    # the lifecycle boundaries explicitly. The writer holds the handle
    # open across every batch (the load + first-write cost happens once,
    # not 50x as the pre-#375 symptom suggested could be reached if a
    # future refactor moved the open inside the loop) and fires
    # incremental saves every ``save_every_n_batches`` batches. The
    # final save fires on ``__exit__`` regardless of cadence.
    #
    # The writer is entered even when ``total == 0`` so the lifecycle
    # is symmetric: every successful ``run_embed`` invocation yields
    # exactly one ``__enter__`` + one ``__exit__`` on the writer,
    # regardless of work to do. This is the contract asserted by
    # ``tests/core/embed/test_embed_vec_index_handle_lifecycle.py``.
    if vec_writer is None:
        vec_index = deps.open_usearch_index()
        _maybe_clear_vec_index_for_force(force, vec_index)
        vec_writer = VecIndexBatchWriter(vec_index, save_every_n_batches=save_every_n_batches)

    if total == 0:
        with vec_writer:
            logger.info("Nothing to embed — index is up to date.")
        return {
            "embedded": 0,
            "skipped": 0,
            "failed": 0,
            "duration_s": 0,
            "estimated_cost_usd": 0.0,
        }

    logger.info(
        "Embedding %d chunks across %d documents (batch_size=%d, parallel=%d)",
        total,
        doc_count,
        batch_size,
        parallel,
    )

    start_time = time.time()
    now = int(start_time)

    cache = deps.open_embedding_cache()

    # #475 — bounded outer 429 retry around every provider call. A
    # RateLimitError that escapes SDK retries waits per Retry-After (or
    # exponential backoff) instead of killing the run with a raw traceback.
    embed_batch_fn = with_rate_limit_retry(deps.embed_batch, sleep_fn=deps.rate_limit_sleep)

    try:
        with vec_writer:
            if parallel == 1:
                embedded, failed_chunks = _run_embed_loop_serial(
                    all_chunks,
                    batch_size,
                    db,
                    vec_writer,
                    api_key,
                    endpoint,
                    deployment,
                    actual_dims,
                    now,
                    total,
                    embed_batch_fn,
                    cache=cache,
                    wal_checkpoint_every_n_batches=wal_checkpoint_every_n_batches,
                )
            else:
                embedded, failed_chunks = _run_embed_loop_parallel(
                    all_chunks,
                    batch_size,
                    parallel,
                    db,
                    vec_writer,
                    api_key,
                    endpoint,
                    deployment,
                    actual_dims,
                    now,
                    total,
                    embed_batch_fn,
                    cache=cache,
                    wal_checkpoint_every_n_batches=wal_checkpoint_every_n_batches,
                )
    finally:
        if cache is not None:
            cache.close()

    duration_s = time.time() - start_time
    estimated_tokens = embedded * 200
    estimated_cost = (estimated_tokens / 1000) * 0.00013

    chunk_date_count = _log_embed_outcome(all_chunks, failed_chunks, total)

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
