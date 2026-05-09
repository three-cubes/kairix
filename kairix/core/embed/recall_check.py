"""Post-embed recall quality gate.

Runs recall queries against the usearch vector index to detect silent
degradation (wrong dims, corrupt vectors, missing documents). Writes
results to ``~/.cache/kairix/recall-check.json`` and alerts when the score
drops more than ``DEGRADATION_THRESHOLD`` from the previous run.

Adaptive mode: when the database is provided and contains indexed
documents, the recall queries are derived from a random sample of
document titles. Otherwise the static ``DEFAULT_RECALL_QUERIES`` are used.

The ``RecallChecker`` class is the only seam: tests inject a ``FakeEmbedProvider``
and a ``FakeVectorSearcher`` via the constructor; production callers build
``RecallChecker()`` which constructs production defaults lazily on first use.
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
    from kairix.platform.llm.embed_provider import EmbedProvider

logger = logging.getLogger(__name__)

RECALL_LOG = Path.home() / ".cache" / "kairix" / "recall-check.json"

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


def _get_recall_queries(db: sqlite3.Connection | None = None) -> list[tuple[str, str, str]]:
    """Return recall queries — adaptive corpus sample if the DB has documents, otherwise defaults."""
    if db is not None:
        adaptive = _build_adaptive_queries(db)
        if adaptive:
            return adaptive
    return list(DEFAULT_RECALL_QUERIES)


def _build_adaptive_queries(db: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """Build recall queries from a random sample of indexed document titles."""
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


def _embed_query(
    query: str,
    *,
    provider: EmbedProvider | None = None,
    model: str | None = None,
) -> np.ndarray | None:
    """Embed a single query string via the configured EmbedProvider.

    Returns a unit-normalised float32 numpy array, or None when credentials
    are missing or the provider call fails. The provider's openai SDK client
    handles retry, rate-limiting, and backoff internally.
    """
    if provider is None:
        try:
            from kairix.credentials import Credentials, get_credentials
            from kairix.platform.llm.embed_provider import get_embed_provider

            creds = get_credentials("embed")
        except Exception:
            logger.warning("Embed credentials not set — skipping recall check")
            return None

        # Reachable only with valid Azure embed credentials; deferred to
        # FakeCredentials in credentials-DI.
        if not isinstance(creds, Credentials) or not creds.api_key or not creds.endpoint:  # pragma: no cover
            logger.warning("Embed credentials not set — skipping recall check")
            return None

        if model is None:  # pragma: no cover — same path as above; needs FakeCredentials
            model = creds.model or "text-embedding-3-large"

        try:  # pragma: no cover — same path as above; needs FakeCredentials
            provider = get_embed_provider()
        except Exception as e:  # pragma: no cover
            logger.warning("Recall embed failed: get_embed_provider raised %s", e)
            return None
    elif model is None:
        model = "text-embedding-3-large"

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


class _UsearchVectorSearcher:  # pragma: no cover
    """Production VectorSearcher — pragma'd whole.

    Tests inject ``FakeVectorSearcher`` via ``RecallChecker(vector_searcher=...)``.
    The real usearch index lookup is exercised by integration coverage with a
    populated usearch index, not by the unit suite.
    """

    def search_vectors(self, vector: np.ndarray, *, limit: int) -> list[str]:
        try:
            from kairix.core.search.vec_index import get_vector_index

            index = get_vector_index()
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

    Constructor takes the two protocol implementations exercised by the gate:

      - ``embed_provider``: any ``EmbedProvider`` (production: Azure/OpenAI;
        tests: ``FakeEmbedProvider``).
      - ``vector_searcher``: any ``VectorSearcher`` (production:
        ``_UsearchVectorSearcher`` wrapping the usearch index; tests:
        ``FakeVectorSearcher`` from ``tests/fakes.py``).

    Both are optional — when ``None`` the production defaults are
    constructed lazily so a bare ``RecallChecker()`` runs against the real
    Azure/usearch surface.
    """

    def __init__(
        self,
        *,
        embed_provider: EmbedProvider | None = None,
        vector_searcher: VectorSearcher | None = None,
    ) -> None:
        self._embed_provider = embed_provider
        self._vector_searcher = vector_searcher

    def _embed(self, query: str) -> np.ndarray | None:
        return _embed_query(query, provider=self._embed_provider)

    def _search(self, vector: np.ndarray, limit: int) -> list[str]:
        searcher = self._vector_searcher
        # Lazy default is production-only — tests inject FakeVectorSearcher via
        # the constructor, so this construction never fires in the unit suite.
        if searcher is None:  # pragma: no cover
            searcher = _UsearchVectorSearcher()
            self._vector_searcher = searcher
        return searcher.search_vectors(vector, limit=limit)

    def check(
        self,
        *,
        db: sqlite3.Connection | None = None,
        recall_queries: list[tuple[str, str, str]] | None = None,
    ) -> dict[str, Any]:
        """Run the recall check.

        Returns ``{score, passed, total, timestamp, detail}`` where
        ``score`` is the fraction of non-skipped queries whose gold fragment
        appeared in the top-k results.
        """
        close_db = False
        if db is None:
            try:
                from kairix.core.db import get_db_path, open_db

                db = open_db(Path(get_db_path()))
                close_db = True
            # ``get_db_path`` returns a path even when missing and ``open_db``
            # auto-creates parent dirs; this guard is defensive for unwritable
            # parents (e.g. read-only fs).
            except FileNotFoundError:  # pragma: no cover
                db = None

        queries = recall_queries if recall_queries is not None else _get_recall_queries(db)
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
) -> dict[str, Any]:
    """Production-default shim — see ``RecallChecker.check``."""
    return RecallChecker().check(db=db, recall_queries=recall_queries)


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
    # NOSONAR — internal log path; not user-controlled (see docstring).
    log_path.write_text(json.dumps(runs, indent=2))


def run_recall_gate(
    alert_callback: Callable[[str], None] | None = None,
    *,
    checker: RecallChecker | None = None,
    log_path: Path = RECALL_LOG,
) -> tuple[bool, dict[str, Any]]:
    """Run the recall gate end-to-end.

    Returns ``(passed, result_dict)``. When the score has dropped more
    than ``DEGRADATION_THRESHOLD`` since the previous run the gate fails
    and ``alert_callback`` is invoked with a human-readable message.

    ``checker`` and ``log_path`` are injection seams used by tests. Production
    leaves them as defaults — ``RecallChecker()`` resolves to Azure + usearch.
    """
    if checker is None:
        checker = RecallChecker()
    result = checker.check()
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
