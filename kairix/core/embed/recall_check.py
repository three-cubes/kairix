"""Post-embed recall quality gate.

Runs recall queries against the usearch vector index to detect silent
degradation (wrong dims, corrupt vectors, missing documents). Writes
results to ``~/.cache/kairix/recall-check.json`` and alerts when the score
drops more than ``DEGRADATION_THRESHOLD`` from the previous run.

Adaptive mode: when the database is provided and contains indexed
documents, the recall queries are derived from a random sample of
document titles. The sample is **persisted** to
``~/.cache/kairix/recall-canaries.json`` on first build and reused on
every subsequent run. Without persistence, each run picks a different
random sample and the run-over-run delta is meaningless — that was
the design bug behind the worker restart-loop fixed in v2026.5.10.

The static ``DEFAULT_RECALL_QUERIES`` are used when the database has
no indexed documents (no corpus to sample from).

To force a re-sample after major corpus changes, delete the canary
cache file or pass ``rebuild_canaries=True`` to ``check()``.

The ``RecallChecker`` class is the only seam: tests inject a
``FakeEmbedProvider`` and a ``FakeVectorSearcher`` via the constructor;
production callers build ``RecallChecker()`` which constructs production
defaults lazily on first use. For unit-coverage of the credentials /
provider-construction failure paths, tests inject a ``creds_resolver``
and ``provider_factory`` via the constructor (``FakeCredentials`` from
``tests/fakes.py`` is the canonical creds stand-in).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import numpy as np

from kairix.core.embed.schema import EMBED_VECTOR_DIMS as EMBED_DIMS

if TYPE_CHECKING:
    from kairix.credentials import Credentials, GraphCredentials
    from kairix.platform.llm.embed_provider import EmbedProvider

logger = logging.getLogger(__name__)

RECALL_LOG = Path.home() / ".cache" / "kairix" / "recall-check.json"
CANARY_CACHE = Path.home() / ".cache" / "kairix" / "recall-canaries.json"
CANARY_CACHE_VERSION = 1

# Static fallback recall queries, used when the database has no indexed
# documents. Each tuple is (id, query, expected_title_fragment).
DEFAULT_RECALL_QUERIES: list[tuple[str, str, str]] = [
    ("R01", "architecture decision record", "architecture"),
    ("R02", "how to deploy", "deploy"),
    ("R03", "testing strategy", "test"),
    ("R04", "search query", "search"),
    ("R05", "project documentation", "project"),
]

DEGRADATION_THRESHOLD = 0.10  # alert if score drops more than 10%
RECALL_LIMIT = 5  # top-k results to check for gold hit
ADAPTIVE_SAMPLE_SIZE = 5  # number of documents to sample for adaptive queries


@runtime_checkable
class VectorSearcher(Protocol):
    """Vector-similarity search seam used by ``RecallChecker``.

    Implementations return document paths in similarity order.
    """

    def search_vectors(self, vector: np.ndarray, *, limit: int) -> list[str]: ...


def get_recall_queries(
    db: sqlite3.Connection | None = None,
    *,
    cache_path: Path | None = CANARY_CACHE,
    rebuild: bool = False,
) -> list[tuple[str, str, str]]:
    """Return recall queries — load from cache, sample fresh on first run.

    First call: build from a random sample of corpus titles, persist to
    ``cache_path``. Subsequent calls: load from cache so the same queries
    run every cycle (deterministic degradation comparisons).

    ``cache_path=None`` disables the cache entirely (always build fresh)
    — used by tests that exercise the adaptive-sampling logic directly.

    ``rebuild=True`` forces a re-sample (e.g. after major corpus change).
    Falls back to ``DEFAULT_RECALL_QUERIES`` when there is no DB or the
    DB has no indexed documents.
    """
    if cache_path is not None and not rebuild:
        cached = load_canary_cache(cache_path)
        if cached:
            return cached

    if db is not None:
        adaptive = build_adaptive_queries(db)
        if adaptive:
            if cache_path is not None:
                save_canary_cache(adaptive, cache_path)
            return adaptive

    return list(DEFAULT_RECALL_QUERIES)


def build_adaptive_queries(db: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """Build recall queries from a random sample of indexed document titles.

    Called once per canary cache lifetime. The randomness here is fine
    because the result is **persisted** by ``save_canary_cache`` and
    reused across runs — same five queries keep firing until the cache
    is rebuilt.
    """
    try:
        rows = db.execute(
            """
            SELECT path, title FROM documents
            WHERE active = 1 AND title IS NOT NULL AND title != ''
            ORDER BY RANDOM()
            LIMIT ?
            """,
            (ADAPTIVE_SAMPLE_SIZE,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []

    if not rows:
        return []

    queries = []
    for i, (path, title) in enumerate(rows, 1):
        # Use the title as the query, path stem as the gold fragment.
        readable = title.replace("-", " ").replace("_", " ")
        stem = Path(path).stem.lower()
        queries.append((f"A{i:02d}", readable, stem))
    return queries


def load_canary_cache(cache_path: Path = CANARY_CACHE) -> list[tuple[str, str, str]] | None:
    """Load persisted canary queries; return None if cache missing or
    schema-incompatible."""
    if not cache_path.exists():
        return None
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("recall-canaries: cache unreadable; re-building")
        return None
    if not isinstance(payload, dict) or payload.get("version") != CANARY_CACHE_VERSION:
        logger.warning("recall-canaries: cache schema mismatch; re-building")
        return None
    queries = payload.get("queries", [])
    if not isinstance(queries, list) or not queries:
        return None
    out: list[tuple[str, str, str]] = []
    for entry in queries:
        try:
            out.append((str(entry["id"]), str(entry["query"]), str(entry["gold_fragment"])))
        except (KeyError, TypeError):
            return None
    return out


def save_canary_cache(
    queries: list[tuple[str, str, str]],
    cache_path: Path = CANARY_CACHE,
    *,
    corpus_size: int | None = None,
) -> None:
    """Persist canary queries to disk.

    The persisted document includes a schema version, a timestamp, and
    optional ``corpus_size`` so future readers can decide whether the
    cache is still meaningful or warrants a rebuild.
    """
    payload = {
        "version": CANARY_CACHE_VERSION,
        "created_at": int(time.time()),
        "corpus_size_at_creation": corpus_size,
        "queries": [{"id": qid, "query": q, "gold_fragment": gf} for qid, q, gf in queries],
    }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info("recall-canaries: cached %d queries to %s", len(queries), cache_path)


def _default_creds_resolver() -> Credentials | GraphCredentials | None:
    """Production credentials resolver — looks up the embed creds.

    Returns whatever ``get_credentials("embed")`` returns; on failure
    (raising) returns ``None`` so the recall check degrades to a skip
    instead of crashing.
    """
    try:
        from kairix.credentials import get_credentials

        return get_credentials("embed")
    except Exception:
        return None


def _default_provider_factory(creds: Credentials) -> EmbedProvider:
    """Production EmbedProvider factory — wraps ``get_embed_provider``.

    Accepts the resolved credentials (unused by ``get_embed_provider`` but
    exposed for symmetry with test injection).
    """
    del creds  # production helper resolves its own credentials internally
    from kairix.platform.llm.embed_provider import get_embed_provider

    return get_embed_provider()


def _resolve_provider_and_model(
    creds_resolver: Callable[[], Credentials | GraphCredentials | None],
    provider_factory: Callable[[Credentials], EmbedProvider],
    model: str | None,
) -> tuple[EmbedProvider | None, str | None]:
    """Resolve an EmbedProvider + model from injected dependencies.

    Returns ``(None, None)`` when credentials are missing / unusable, the
    resolver itself raises, or the factory raises — callers treat this as
    a skip-the-query signal. The recall gate is an alarm system; it must
    degrade silently rather than take down the embed pipeline.
    """
    from kairix.credentials import Credentials

    try:
        creds = creds_resolver()
    except Exception as e:
        logger.warning("Recall embed: creds_resolver raised %s — skipping", e)
        return None, None

    if not isinstance(creds, Credentials) or not creds.api_key or not creds.endpoint:
        logger.warning("Embed credentials not set — skipping recall check")
        return None, None

    chosen_model = model if model is not None else (creds.model or "text-embedding-3-large")

    try:
        provider = provider_factory(creds)
    except Exception as e:
        logger.warning("Recall embed failed: provider_factory raised %s", e)
        return None, None
    return provider, chosen_model


def _embed_query(
    query: str,
    *,
    provider: EmbedProvider,
    model: str = "text-embedding-3-large",
) -> np.ndarray | None:
    """Embed a single query string via the supplied EmbedProvider.

    Returns a unit-normalised float32 numpy array, or ``None`` when the
    provider call fails or returns no vectors. The provider's openai SDK
    client handles retry, rate-limiting, and backoff internally.

    This helper is intentionally pure — credentials resolution and provider
    construction live in ``_resolve_provider_and_model`` (called by
    ``RecallChecker._embed``) so the embed-batch call path can be tested
    with a ``FakeEmbedProvider`` without exercising the credentials seam.
    """
    try:
        vectors = provider.embed_batch([query], model=model, dims=EMBED_DIMS)
        if not vectors:
            return None
        arr = np.array(vectors[0], dtype=np.float32)
        norm = np.linalg.norm(arr)
        if norm > 0:
            arr /= norm
        return arr
    except Exception as e:
        logger.warning("Recall embed failed for query '%s': %s", query[:40], e)
        return None


def _default_index_resolver() -> Any:
    """Production index resolver — wraps ``get_vector_index`` so tests can inject a fake."""
    from kairix.core.search.vec_index import get_vector_index

    return get_vector_index()


class UsearchVectorSearcher:
    """Production VectorSearcher wrapping the usearch index.

    Tests construct it with ``index_resolver=`` to drive every branch
    (index missing, search returns hits, search raises). Production callers
    leave ``index_resolver=None`` and the default
    (``kairix.core.search.vec_index.get_vector_index``) is used.
    """

    def __init__(self, index_resolver: Callable[[], Any] | None = None) -> None:
        self._index_resolver = index_resolver if index_resolver is not None else _default_index_resolver

    def search_vectors(self, vector: np.ndarray, *, limit: int) -> list[str]:
        try:
            index = self._index_resolver()
            if index is None:
                logger.warning("usearch index not available for recall check")
                return []
            results = index.search(vector, k=limit)
            return [r["path"] for r in results]
        except Exception as e:
            logger.warning("usearch recall search failed: %s", e)
            return []


class RecallChecker:
    """Embed-and-vector-search recall quality gate.

    Constructor takes the two protocol implementations exercised by the gate
    plus injection seams for the credentials / provider lookup:

      - ``embed_provider``: any ``EmbedProvider`` (production: Azure/OpenAI;
        tests: ``FakeEmbedProvider``). When ``None``, ``creds_resolver`` and
        ``provider_factory`` are used to construct one on first ``_embed`` call.
      - ``vector_searcher``: any ``VectorSearcher`` (production:
        ``UsearchVectorSearcher`` wrapping the usearch index; tests:
        ``FakeVectorSearcher`` from ``tests/fakes.py``).
      - ``creds_resolver``: returns a ``Credentials`` or ``None``. Default
        delegates to ``kairix.credentials.get_credentials("embed")`` and
        treats any exception as "no credentials" (returns ``None``).
      - ``provider_factory``: builds an ``EmbedProvider`` from resolved
        ``Credentials``. Default delegates to
        ``kairix.platform.llm.embed_provider.get_embed_provider``.

    All four are optional — production callers leave them all None and the
    real Azure / usearch surface is used. Tests inject ``FakeEmbedProvider``
    + ``FakeVectorSearcher`` to drive the happy paths, and inject
    ``FakeCredentials`` (via ``creds_resolver``) to exercise the
    credentials-resolution failure paths without touching real secrets.
    """

    def __init__(
        self,
        *,
        embed_provider: EmbedProvider | None = None,
        vector_searcher: VectorSearcher | None = None,
        creds_resolver: Callable[[], Credentials | GraphCredentials | None] | None = None,
        provider_factory: Callable[[Credentials], EmbedProvider] | None = None,
    ) -> None:
        self._embed_provider = embed_provider
        self._vector_searcher = vector_searcher
        self._creds_resolver = creds_resolver if creds_resolver is not None else _default_creds_resolver
        self._provider_factory = provider_factory if provider_factory is not None else _default_provider_factory
        # Cached resolved model so we only resolve credentials once per checker.
        self._resolved_model: str | None = None

    def _embed(self, query: str) -> np.ndarray | None:
        provider = self._embed_provider
        model: str
        if provider is None:
            resolved_provider, resolved_model = _resolve_provider_and_model(
                self._creds_resolver,
                self._provider_factory,
                self._resolved_model,
            )
            if resolved_provider is None or resolved_model is None:
                return None
            self._embed_provider = resolved_provider
            self._resolved_model = resolved_model
            provider = resolved_provider
            model = resolved_model
        else:
            model = self._resolved_model or "text-embedding-3-large"
        return _embed_query(query, provider=provider, model=model)

    def _search(self, vector: np.ndarray, limit: int) -> list[str]:
        searcher = self._vector_searcher
        if searcher is None:
            searcher = UsearchVectorSearcher()
            self._vector_searcher = searcher
        return searcher.search_vectors(vector, limit=limit)

    def check(
        self,
        *,
        db: sqlite3.Connection | None = None,
        recall_queries: list[tuple[str, str, str]] | None = None,
        canary_cache_path: Path | None = CANARY_CACHE,
        rebuild_canaries: bool = False,
    ) -> dict[str, Any]:
        """Run the recall check.

        Returns ``{score, passed, total, timestamp, detail}`` where
        ``score`` is the fraction of non-skipped queries whose gold fragment
        appeared in the top-k results.

        ``recall_queries`` lets tests inject a deterministic suite. In
        production it is None and the queries come from the persistent
        canary cache (``canary_cache_path``); the cache is built on
        first run from a corpus sample and reused thereafter so the
        run-over-run delta is meaningful. Pass ``rebuild_canaries=True``
        to discard the cache and re-sample.
        """
        close_db = False
        if db is None:
            from kairix.core.db import get_db_path, open_db

            # ``get_db_path`` returns a path (existing or default-creation
            # location) and ``open_db`` auto-creates parent dirs and the
            # SQLite file via ``sqlite3.connect``, so neither raises
            # ``FileNotFoundError``. Other exceptions (e.g. permission
            # errors) are surfaced — they indicate broken environment
            # config that the gate cannot work around.
            db = open_db(Path(get_db_path()))
            close_db = True

        if recall_queries is not None:
            queries = recall_queries
        else:
            queries = get_recall_queries(db, cache_path=canary_cache_path, rebuild=rebuild_canaries)
        passed = 0
        detail: list[dict[str, Any]] = []

        for qid, query, gold_fragment in queries:
            query_vec = self._embed(query)
            if query_vec is None:
                detail.append(
                    {
                        "id": qid,
                        "query": query,
                        "gold_fragment": gold_fragment,
                        "hit": False,
                        "returned": [],
                        "skipped": True,
                    }
                )
                continue

            files = self._search(query_vec, RECALL_LIMIT)
            hit = any(gold_fragment.lower() in f.lower() for f in files)
            if hit:
                passed += 1
            detail.append(
                {
                    "id": qid,
                    "query": query,
                    "gold_fragment": gold_fragment,
                    "hit": hit,
                    "returned": files,
                    "skipped": False,
                }
            )

        if close_db and db is not None:
            db.close()

        checked = sum(1 for d in detail if not d.get("skipped"))
        score = passed / checked if checked > 0 else 0.0
        return {
            "score": round(score, 4),
            "passed": passed,
            "total": checked,
            "timestamp": int(time.time()),
            "detail": detail,
        }


def check_recall(
    db: sqlite3.Connection | None = None,
    *,
    recall_queries: list[tuple[str, str, str]] | None = None,
    rebuild_canaries: bool = False,
) -> dict[str, Any]:
    """Production-default shim — see ``RecallChecker.check``."""
    return RecallChecker().check(db=db, recall_queries=recall_queries, rebuild_canaries=rebuild_canaries)


def load_previous_score(log_path: Path = RECALL_LOG) -> float | None:
    """Load the most recent recall score from the log."""
    if not log_path.exists():
        return None
    try:
        runs = json.loads(log_path.read_text())
        if runs:
            return float(runs[-1].get("score", 0.0))
    except (json.JSONDecodeError, OSError, KeyError):
        pass
    return None


def save_recall_result(result: dict[str, Any], log_path: Path = RECALL_LOG) -> None:
    """Append recall result to the log. Keep last 90 entries.

    ``log_path`` is a kairix-internal path. The default
    (``~/.cache/kairix/recall-check.json``) is fixed at module load; tests
    inject a tmp_path-scoped path. The parameter is not exposed via any
    user-facing CLI flag — it is an internal injection seam, not user
    input.
    """
    runs: list[dict[str, Any]] = []
    if log_path.exists():
        try:
            runs = json.loads(log_path.read_text())
        except (json.JSONDecodeError, OSError):
            runs = []
    runs.append(result)
    runs = runs[-90:]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(json.dumps(runs, indent=2))  # NOSONAR — internal log path; not user-controlled (see docstring).


def run_recall_gate(
    alert_callback: Callable[[str], None] | None = None,
    *,
    checker: RecallChecker | None = None,
    log_path: Path = RECALL_LOG,
    rebuild_canaries: bool = False,
) -> tuple[bool, dict[str, Any]]:
    """Run the recall gate end-to-end.

    Returns ``(passed, result_dict)``. When the score has dropped more
    than ``DEGRADATION_THRESHOLD`` since the previous run the gate fails
    and ``alert_callback`` is invoked with a human-readable message.

    The canary suite is sourced from a persistent on-disk cache so the
    same queries fire on every run — see ``get_recall_queries``. Pass
    ``rebuild_canaries=True`` (or run ``kairix embed --rebuild-canaries``)
    to discard the cache and re-sample.

    ``checker`` and ``log_path`` are injection seams used by tests. Production
    leaves them as defaults — ``RecallChecker()`` resolves to Azure + usearch.
    """
    if checker is None:
        checker = RecallChecker()
    result = checker.check(rebuild_canaries=rebuild_canaries)
    prev_score = load_previous_score(log_path)
    save_recall_result(result, log_path)

    score = result["score"]
    logger.info("Recall check: %d/%d (%.0f%%)", result["passed"], result["total"], score * 100)

    if prev_score is not None:
        delta = score - prev_score
        if delta < -DEGRADATION_THRESHOLD:
            msg = (
                f"Recall degraded: {score:.0%} (was {prev_score:.0%}, delta {delta:+.0%}). "
                "Check azure-embed.log and run kairix onboard check."
            )
            logger.warning(msg)
            if alert_callback:
                alert_callback(msg)
            return False, result

    return True, result
