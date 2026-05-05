"""
Post-embed recall quality gate.

Runs recall queries against the usearch vector index to detect silent
degradation (wrong dims, corrupt vectors, missing documents).
Writes results to ~/.cache/kairix/recall-check.json.
Alerts if score drops >10% from previous run.

Adaptive mode: when no RECALL_QUERIES env var is set, derives recall
queries from the indexed documents themselves (random sample of titles).
"""

import json
import logging
import os
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from kairix.core.embed.schema import EMBED_VECTOR_DIMS as EMBED_DIMS

logger = logging.getLogger(__name__)

RECALL_LOG = Path.home() / ".cache" / "kairix" / "recall-check.json"

# Fallback recall queries when no indexed documents exist and no env var is set.
# Each tuple: (id, query, expected_title_fragment).
DEFAULT_RECALL_QUERIES = [
    ("R01", "architecture decision record", "architecture"),
    ("R02", "how to deploy", "deploy"),
    ("R03", "testing strategy", "test"),
    ("R04", "search query", "search"),
    ("R05", "project documentation", "project"),
]

DEGRADATION_THRESHOLD = 0.10  # alert if score drops more than 10%
RECALL_LIMIT = 5  # top-k results to check for gold hit
ADAPTIVE_SAMPLE_SIZE = 5  # number of documents to sample for adaptive queries


def _get_recall_queries(db: sqlite3.Connection | None = None) -> list[tuple[str, str, str]]:
    """Return recall queries — from env var, adaptive corpus sample, or defaults.

    Priority:
      1. RECALL_QUERIES env var (JSON array of [id, query, expected_fragment])
      2. Adaptive: random sample of indexed document titles
      3. Static defaults
    """
    env = os.environ.get("RECALL_QUERIES")
    if env:
        try:
            return [tuple(q) for q in json.loads(env)]
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.warning("RECALL_QUERIES env var is not valid JSON — trying adaptive")

    # Adaptive: sample titles from the index
    if db is not None:
        adaptive = _build_adaptive_queries(db)
        if adaptive:
            return adaptive

    return DEFAULT_RECALL_QUERIES


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
        # Use the title as the query, path stem as the gold fragment
        readable = title.replace("-", " ").replace("_", " ")
        stem = Path(path).stem.lower()
        queries.append((f"A{i:02d}", readable, stem))

    return queries


def _embed_query(query: str, *, provider: Any = None, model: str | None = None) -> np.ndarray | None:
    """Embed a single query string via the configured EmbedProvider.

    Returns a normalised float32 numpy array, or None when credentials are
    missing / the provider call fails. The provider's openai SDK client
    handles retry, rate-limiting, and backoff internally.

    Args:
        query:    The text to embed.
        provider: Optional EmbedProvider for testing. When None, resolves
                  via get_embed_provider() using configured credentials.
        model:    Optional model deployment name. When None, derives from
                  configured credentials (falling back to text-embedding-3-large).
    """
    if provider is None:
        try:
            from kairix.credentials import Credentials, get_credentials
            from kairix.platform.llm.embed_provider import get_embed_provider

            creds = get_credentials("embed")
        except Exception:
            creds = None
            get_embed_provider = None  # type: ignore[assignment]

        if not isinstance(creds, Credentials) or not creds.api_key or not creds.endpoint:
            logger.warning("Embed credentials not set — skipping recall check")
            return None

        if model is None:
            model = creds.model or "text-embedding-3-large"

        try:
            provider = get_embed_provider()
        except Exception as e:
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


def _vsearch_usearch(query_vec: np.ndarray, limit: int = RECALL_LIMIT) -> list[str]:
    """Run vector similarity search via usearch index.

    Returns list of document paths in similarity order.
    """
    try:
        from kairix.core.search.hybrid import get_vector_index

        index = get_vector_index()
        if index is None:
            logger.warning("usearch index not available for recall check")
            return []

        results = index.search(query_vec, k=limit)
        return [r["path"] for r in results]
    except Exception as e:
        logger.warning("usearch recall search failed: %s", e)
        return []


def check_recall(
    db: sqlite3.Connection | None = None,
    *,
    embed_fn: Callable[[str], np.ndarray | None] | None = None,
    vsearch_fn: Callable[[np.ndarray, int], list[str]] | None = None,
    recall_queries: list[tuple[str, str, str]] | None = None,
) -> dict[str, Any]:
    """
    Run recall check queries via usearch vector search.
    Returns {score, passed, total, detail}.
    Score is fraction of queries where gold path fragment appears in top-5.

    If db is None, opens the kairix DB internally (for adaptive query building).

    Args:
        db: SQLite connection for adaptive query building.
        embed_fn: Callable to embed a query string. Defaults to _embed_query.
        vsearch_fn: Callable to run vector search. Defaults to _vsearch_usearch.
        recall_queries: Override recall queries (skip adaptive/default logic).
    """
    if embed_fn is None:
        embed_fn = _embed_query
    if vsearch_fn is None:
        vsearch_fn = _vsearch_usearch

    close_db = False
    if db is None:
        try:
            from kairix.core.db import get_db_path, open_db

            db = open_db(Path(get_db_path()))
            close_db = True
        except FileNotFoundError:
            db = None

    queries = recall_queries if recall_queries is not None else _get_recall_queries(db)
    passed = 0
    detail = []

    for qid, query, gold_fragment in queries:
        query_vec = embed_fn(query)
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

        files = vsearch_fn(query_vec, RECALL_LIMIT)
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


def load_previous_score() -> float | None:
    """Load the most recent recall score from the log."""
    if not RECALL_LOG.exists():
        return None
    try:
        runs = json.loads(RECALL_LOG.read_text())
        if runs:
            return float(runs[-1].get("score", 0.0))
    except (json.JSONDecodeError, OSError, KeyError):
        pass
    return None


def save_recall_result(result: dict[str, Any]) -> None:
    """Append recall result to the log. Keep last 90 entries."""
    runs = []
    if RECALL_LOG.exists():
        try:
            runs = json.loads(RECALL_LOG.read_text())
        except (json.JSONDecodeError, OSError):
            runs = []
    runs.append(result)
    runs = runs[-90:]
    RECALL_LOG.write_text(json.dumps(runs, indent=2))


def run_recall_gate(
    alert_callback: Callable[[str], None] | None = None,
) -> tuple[bool, dict[str, Any]]:
    """
    Run the recall gate. Returns (passed, result_dict).

    If score dropped >DEGRADATION_THRESHOLD vs previous run, calls alert_callback(message).
    The alert_callback is injected by the CLI (e.g. Telegram notify).
    """
    result = check_recall()
    prev_score = load_previous_score()
    save_recall_result(result)

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
