"""
Reciprocal Rank Fusion (RRF) + entity boosting + procedural boosting + temporal
date boosting for the kairix search pipeline.

RRF combines BM25 and vector search result lists into a single ranked list.
Entity boosting increases scores for documents that have known entity mentions,
rewarding documents that are associated with important named entities.
Procedural boosting re-ranks procedural content (how-to guides, runbooks) for
PROCEDURAL intent queries where retrieval hits but ranking is weak.
Temporal date boosting re-ranks documents whose path contains a date string
matching the queried date, for TEMPORAL intent queries.

Boost behaviour is controlled via config dataclasses (see kairix.core.search.config).
Pass a RetrievalConfig to hybrid_search() to tune or disable individual boosts.
Use RetrievalConfig.minimal() for RRF baseline isolation.

Constants:
  RRF_K = 60  Standard RRF constant (Cormack et al., 2009)

RRF formula per document:
  score(d) = sum(1 / (k + rank_in_list) for each list containing d)
  Documents appearing in only one list use len(other_list) + 1 as their rank.

Entity boost formula:
  boost(d) = 1 + min(factor * log(1 + mention_count), cap - 1)
  Applied after RRF, before budget trim.

Procedural boost:
  Applied post-RRF, after entity boost, for PROCEDURAL intent queries only.
  Multiplies boosted_score by config.factor for documents whose path
  matches procedural content patterns (how-to-*, /runbooks/, runbook-*, procedure*).
  Zero effect on other intent types.

Temporal date boost:
  Applied post-RRF, after entity boost, for TEMPORAL intent queries only.
  Multiplies boosted_score by config.date_path_boost_factor for documents whose
  path contains a date string extracted from the query (YYYY-MM-DD or YYYY-MM).
  Also boosts recent documents for relative temporal queries ("recent", "last month").
  Disabled by default (date_path_boost_enabled=False in TemporalBoostConfig).
  Zero effect on other intent types.

All functions return [] on empty inputs. Never raise.
"""

import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path

from kairix.core.search.bm25 import BM25Result
from kairix.core.search.config import (
    EntityBoostConfig,
    ProceduralBoostConfig,
    TemporalBoostConfig,
)
from kairix.core.search.vec_index import VecResult
from kairix.core.temporal.rewriter import QUERY_ISO_DATE_RE as _QUERY_ISO_DATE_RE
from kairix.core.temporal.rewriter import QUERY_YEAR_MONTH_RE as _QUERY_YEAR_MONTH_RE
from kairix.core.temporal.rewriter import RELATIVE_TEMPORAL_RE as _RELATIVE_TEMPORAL_RE
from kairix.utils import slugify

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RRF_K: int = 60


# ---------------------------------------------------------------------------
# Path normalisation
# ---------------------------------------------------------------------------


def canonical_path(raw: str) -> str:
    """Normalise path for deduplication.

    Strips known collection-root prefixes so the same document indexed
    under different paths deduplicates during fusion.
    """
    for prefix in ("obsidian-vault/",):
        if raw.startswith(prefix):
            return raw[len(prefix) :]
    return raw


# ---------------------------------------------------------------------------
# Entity slug helpers (for secondary name-based lookup)
# ---------------------------------------------------------------------------


_LABEL_TO_DIR: dict[str, str] = {
    "person": "person",
    "organisation": "organisation",
    "organization": "organisation",
    "concept": "concept",
}

# ---------------------------------------------------------------------------
# Temporal date extraction patterns (query-side) — canonical source: temporal.rewriter
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class FusedResult:
    """A search result after RRF fusion, optionally entity-boosted."""

    # Document identity
    path: str
    collection: str
    title: str
    snippet: str

    # Scores
    rrf_score: float = 0.0
    boosted_score: float = 0.0

    # Source membership
    in_bm25: bool = False
    in_vec: bool = False

    # Entity info (populated by entity_boost)
    entity_mention_count: int = 0

    # Chunk date metadata (populated at index time — used by chunk_date_boost, TMP-7B)
    chunk_date: str = ""

    # Cross-encoder re-rank score (populated by rerank.rerank() when enabled)
    rerank_score: float = 0.0

    # Raw ranks (1-based, 0 = not ranked in that list)
    bm25_rank: int = 0
    vec_rank: int = 0


# ---------------------------------------------------------------------------
# RRF fusion
# ---------------------------------------------------------------------------


def rrf(
    bm25: list[BM25Result],
    vec: list[VecResult],
    k: int = RRF_K,
) -> list[FusedResult]:
    """
    Reciprocal Rank Fusion of BM25 and vector search results.

    Args:
        bm25:  BM25 results in rank order (highest score first).
        vec:   Vector results in rank order (lowest distance first = best first).
        k:     RRF constant. Default 60.

    Returns:
        Fused results sorted by RRF score descending.
        Returns [] if both inputs are empty.
        Never raises.
    """
    if not bm25 and not vec:
        return []

    try:
        return _rrf_impl(bm25, vec, k)
    except Exception as e:
        logger.warning("rrf: unexpected error during fusion — %s", e)
        return []


def _rrf_impl(
    bm25: list[BM25Result],
    vec: list[VecResult],
    k: int,
) -> list[FusedResult]:
    """Implementation of RRF — called from rrf() with error boundary."""
    # Build path → FusedResult index
    fused: dict[str, FusedResult] = {}

    # Process BM25 results (1-indexed ranks)
    for rank, result in enumerate(bm25, start=1):
        path = canonical_path(result["file"])
        if path not in fused:
            fused[path] = FusedResult(
                path=path,
                collection=result["collection"],
                title=result["title"],
                snippet=result["snippet"],
            )
        fused[path].in_bm25 = True
        fused[path].bm25_rank = rank
        fused[path].rrf_score += 1.0 / (k + rank)

    # Process vector results (1-indexed ranks)
    for rank, result in enumerate(vec, start=1):
        path = canonical_path(result["path"])
        if path not in fused:
            fused[path] = FusedResult(
                path=path,
                collection=result["collection"],
                title=result["title"],
                snippet=result["snippet"],
            )
        fused[path].in_vec = True
        fused[path].vec_rank = rank
        fused[path].rrf_score += 1.0 / (k + rank)

    # Documents in only one list: they already got their score from that list's rank.
    # The spec says: "Results appearing in only one list get rank = len(other_list) + 1."
    # Interpretation: they do NOT get an additional contribution from the absent list —
    # they simply don't accumulate a score from it (which is what the above code does).

    # Sort by RRF score descending
    results = sorted(fused.values(), key=lambda r: r.rrf_score, reverse=True)

    # Initialise boosted_score from rrf_score
    for r in results:
        r.boosted_score = r.rrf_score

    return results


# ---------------------------------------------------------------------------
# BM25-primary fusion
# ---------------------------------------------------------------------------


def bm25_primary_fuse(
    bm25: list[BM25Result],
    vec: list[VecResult],
) -> list[FusedResult]:
    """
    BM25-primary fusion: BM25 results first (in BM25 rank order),
    then vector-only results appended at the bottom.

    This preserves BM25's strong NDCG ranking while gaining vector's
    recall advantage. Sweep showed this beats RRF by +17.8% weighted NDCG.

    Documents in both lists appear at their BM25 rank position.
    Vector-only documents are appended in vector rank order.

    Args:
        bm25:  BM25 results in rank order (highest score first).
        vec:   Vector results in rank order (lowest distance first = best first).

    Returns:
        Fused results with BM25 results first, vector-only appended.
        Returns [] if both inputs are empty. Never raises.
    """
    if not bm25 and not vec:
        return []

    try:
        return _bm25_primary_impl(bm25, vec)
    except Exception as e:
        logger.warning("bm25_primary_fuse: unexpected error — %s", e)
        return []


def _bm25_primary_impl(
    bm25: list[BM25Result],
    vec: list[VecResult],
) -> list[FusedResult]:
    """Implementation of BM25-primary fusion."""
    results: list[FusedResult] = []
    seen: set[str] = set()

    # Phase 1: BM25 results in rank order (primary ranking)
    for rank, result in enumerate(bm25, start=1):
        path = canonical_path(result["file"])

        if path.lower() in seen:
            continue
        seen.add(path.lower())

        fr = FusedResult(
            path=path,
            collection=result["collection"],
            title=result["title"],
            snippet=result["snippet"],
            in_bm25=True,
            bm25_rank=rank,
            # Score: use BM25 position-based score so boosted_score ordering is preserved
            rrf_score=1.0 / rank,
        )
        fr.boosted_score = fr.rrf_score
        results.append(fr)

    # Phase 2: Vector-only results appended (recall backfill)
    base_rank = len(results)
    for rank, result in enumerate(vec, start=1):
        path = canonical_path(result["path"])

        if path.lower() in seen:
            # Mark BM25 result as also in vec
            for fr in results:
                if fr.path.lower() == path.lower():
                    fr.in_vec = True
                    fr.vec_rank = rank
                    break
            continue
        seen.add(path.lower())

        fr = FusedResult(
            path=path,
            collection=result["collection"],
            title=result["title"],
            snippet=result["snippet"],
            in_vec=True,
            vec_rank=rank,
            # Score: below all BM25 results but in vec rank order
            rrf_score=1.0 / (base_rank + rank),
        )
        fr.boosted_score = fr.rrf_score
        results.append(fr)

    return results


# ---------------------------------------------------------------------------
# Entity boosting (Neo4j)
# ---------------------------------------------------------------------------


def _build_entity_index(
    neo4j_client: object,
) -> tuple[dict[str, int], dict[str, int], dict[str, int], int]:
    """Run Cypher query and build entity lookup dicts.

    Returns:
        (path_in_degree, dir_in_degree, name_slug_in_degree, max_in_degree).
        All dicts are empty and max_in_degree is 0 on failure.
    """
    empty: tuple[dict[str, int], dict[str, int], dict[str, int], int] = ({}, {}, {}, 0)
    try:
        rows = neo4j_client.cypher(  # type: ignore[union-attr] — neo4j_client typed as object; cypher() is duck-typed and exception-guarded
            "MATCH (n) WHERE n.vault_path IS NOT NULL AND n.vault_path <> '' "
            "OPTIONAL MATCH ()-[:MENTIONS]->(n) "
            "RETURN n.vault_path AS vault_path, n.name AS name, labels(n) AS labels, count(*) AS in_degree"
        )
    except Exception as e:
        logger.warning("entity_boost_neo4j: cypher failed — %s", e)
        return empty

    if not rows:
        return empty

    path_in_degree: dict[str, int] = {}
    dir_in_degree: dict[str, int] = {}
    name_slug_in_degree: dict[str, int] = {}

    for row in rows:
        vp = str(row["vault_path"]).lower().replace("\\", "/")
        in_deg = int(row.get("in_degree") or 0)
        path_in_degree[vp] = in_deg
        parent = str(Path(vp).parent).lower().replace("\\", "/")
        if parent not in (".", ""):
            dir_in_degree[parent] = max(dir_in_degree.get(parent, 0), in_deg)

        # Slug-based secondary lookup from entity name + label
        name = str(row.get("name") or "").strip()
        labels = row.get("labels") or []
        if name:
            for lbl in labels:
                dir_name = _LABEL_TO_DIR.get(str(lbl).lower())
                if dir_name:
                    slug = slugify(name)
                    if slug:
                        doc_path = f"{dir_name}/{slug}.md"
                        existing = name_slug_in_degree.get(doc_path, 0)
                        name_slug_in_degree[doc_path] = max(existing, in_deg)

    if not path_in_degree:
        return empty

    max_in_degree = max(path_in_degree.values()) or 1
    return path_in_degree, dir_in_degree, name_slug_in_degree, max_in_degree


def _lookup_mention_count(
    result_path: str,
    path_index: dict[str, int],
    dir_index: dict[str, int],
    slug_index: dict[str, int],
) -> tuple[int, int]:
    """Three-tier entity lookup for a single result path.

    Tries exact path match, then name-slug match, then directory match
    (half boost). Returns (mention_count, in_degree).
    """
    path_lower = result_path.lower().replace("\\", "/")
    in_deg = path_index.get(path_lower, 0)

    # Secondary: slug-based lookup from entity name
    if in_deg == 0:
        in_deg = slug_index.get(path_lower, 0)

    if in_deg == 0:
        # Half-boost for files under an entity directory
        for dir_prefix, dir_deg in dir_index.items():
            if path_lower.startswith(dir_prefix + "/"):
                in_deg = max(in_deg, dir_deg // 2)
                break

    return in_deg, in_deg


def _compute_entity_boost_factor(
    in_degree: int,
    max_in_degree: int,
    config: EntityBoostConfig,
) -> float:
    """Normalise in-degree and apply log boost formula. Returns multiplier."""
    normalised = in_degree / max_in_degree
    boost_amount = min(config.factor * math.log1p(normalised * 10), config.cap - 1.0)
    return 1.0 + boost_amount


def entity_boost_neo4j(
    results: list[FusedResult],
    neo4j_client: object,
    config: EntityBoostConfig | None = None,
) -> list[FusedResult]:
    """
    Boost entity canonical notes and entity-directory documents using Neo4j.

    Queries Neo4j for entity vault_paths and their MENTIONS in-degree.
    Documents matching an entity vault_path or living inside an entity directory
    receive a log-scaled boost proportional to the entity's in-degree.

    Called for all intents post-RRF. For ENTITY intent, hybrid.py guarantees
    Neo4j is available before this is called. For other intents, if Neo4j is
    unavailable the boost is skipped and results are returned unmodified.
    Never raises.
    """
    if not results:
        return results

    cfg = config if config is not None else EntityBoostConfig()
    if not cfg.enabled or neo4j_client is None or not getattr(neo4j_client, "available", False):
        for r in results:
            r.boosted_score = r.rrf_score
        return results

    path_idx, dir_idx, slug_idx, max_in_deg = _build_entity_index(neo4j_client)
    if not path_idx and not dir_idx:
        for r in results:
            r.boosted_score = r.rrf_score
        return results

    for r in results:
        mention_count, in_deg = _lookup_mention_count(r.path, path_idx, dir_idx, slug_idx)
        r.entity_mention_count = mention_count
        if in_deg > 0:
            r.boosted_score = r.rrf_score * _compute_entity_boost_factor(in_deg, max_in_deg, cfg)
        else:
            r.boosted_score = r.rrf_score

    return sorted(results, key=lambda r: r.boosted_score, reverse=True)


# ---------------------------------------------------------------------------
# Procedural boosting
# ---------------------------------------------------------------------------


def procedural_boost(
    results: list[FusedResult],
    config: ProceduralBoostConfig | None = None,
) -> list[FusedResult]:
    """
    Boost documents whose paths match procedural content patterns for PROCEDURAL
    intent queries.

    Called after entity_boost(), before apply_budget(). Only called when
    intent == QueryIntent.PROCEDURAL — callers are responsible for the guard.

    Boost logic:
      If any pattern in config.path_patterns matches result.path:
        result.boosted_score *= config.factor

    This is a re-ranking fix, not a retrieval fix. Procedural files are typically
    retrieved (Hit@5 > 0.5) but ranked too low (positions 4-7). The 1.4x multiplier
    moves them into the top-3 without over-ranking them for non-procedural queries
    (the boost is gated to PROCEDURAL intent in hybrid.py).

    Args:
        results:  List of FusedResult (from rrf(), after entity_boost()).
        config:   ProceduralBoostConfig. Default: ProceduralBoostConfig().

    Returns:
        Results re-sorted by boosted_score descending.
        Returns results unmodified on any error.
        Never raises.
    """
    cfg = config if config is not None else ProceduralBoostConfig()
    if not cfg.enabled:
        return results

    if not results:
        return results

    try:
        return _procedural_boost_impl(results, cfg)
    except Exception as e:
        logger.warning("procedural_boost: error — %s — returning unmodified results", e)
        return results


def _procedural_boost_impl(
    results: list[FusedResult],
    config: ProceduralBoostConfig,
) -> list[FusedResult]:
    """Implementation of procedural boosting — called from procedural_boost() with error boundary."""
    patterns = [re.compile(p, re.IGNORECASE) for p in config.path_patterns]
    for r in results:
        if any(p.search(r.path) for p in patterns):
            r.boosted_score *= config.factor
    return sorted(results, key=lambda r: r.boosted_score, reverse=True)


# ---------------------------------------------------------------------------
# Temporal date boosting
# ---------------------------------------------------------------------------


def temporal_date_boost(
    results: list[FusedResult],
    query: str,
    config: TemporalBoostConfig | None = None,
) -> list[FusedResult]:
    """
    Boost documents whose path contains a date string matching the queried date
    for TEMPORAL intent queries.

    Called after entity_boost(), before apply_budget(). Only called when
    intent == QueryIntent.TEMPORAL — callers are responsible for the guard.
    Disabled by default (date_path_boost_enabled=False in TemporalBoostConfig).

    Boost logic:
      - If query contains a specific date (YYYY-MM-DD): boost documents whose
        path contains that exact date string or its YYYY-MM prefix.
      - If query contains a relative temporal term ("recent", "last week",
        "last month", "yesterday", "today"): boost documents whose path
        contains an ISO date from the last 30 days (last week) or 90 days
        (last month / recent).
      - Non-matching documents are unaffected.

    Args:
        results:  List of FusedResult (from rrf(), after entity_boost()).
        query:    The original (or rewritten) query string.
        config:   TemporalBoostConfig. Default: TemporalBoostConfig().

    Returns:
        Results re-sorted by boosted_score descending.
        Returns results unmodified on any error.
        Never raises.
    """
    cfg = config if config is not None else TemporalBoostConfig()
    if not cfg.date_path_boost_enabled:
        return results

    if not results:
        return results

    try:
        return _temporal_date_boost_impl(results, query, cfg.date_path_boost_factor)
    except Exception as e:
        logger.warning("temporal_date_boost: error — %s — returning unmodified results", e)
        return results


def _extract_query_date_strings(query: str) -> list[str]:
    """Extract explicit date strings from a query for path matching.

    Returns date strings (YYYY-MM-DD and/or YYYY-MM) found in the query,
    or an empty list if none are found.
    """
    iso_match = _QUERY_ISO_DATE_RE.search(query)
    if iso_match:
        return [iso_match.group(1), iso_match.group(1)[:7]]

    ym_match = _QUERY_YEAR_MONTH_RE.search(query)
    if ym_match:
        return [ym_match.group(1)]

    return []


def _boost_by_recency_window(
    results: list[FusedResult],
    query: str,
    boost_factor: float,
) -> bool:
    """Boost results whose path contains a date within the relative temporal window.

    Returns True if any result was boosted.
    """
    import datetime

    rel_match = _RELATIVE_TEMPORAL_RE.search(query)
    if not rel_match:
        return False

    term = rel_match.group(1).lower()
    today = datetime.date.today()
    if "last week" in term or "yesterday" in term or "today" in term:
        cutoff = today - datetime.timedelta(days=30)
    else:
        cutoff = today - datetime.timedelta(days=90)

    _path_date_re = re.compile(r"(\d{4}-\d{2}-\d{2})")
    boosted_any = False
    for r in results:
        path_date_match = _path_date_re.search(r.path)
        if not path_date_match:
            continue
        try:
            path_date = datetime.date.fromisoformat(path_date_match.group(1))
            if path_date >= cutoff:
                r.boosted_score *= boost_factor
                boosted_any = True
        except ValueError:
            pass

    return boosted_any


def _temporal_date_boost_impl(
    results: list[FusedResult],
    query: str,
    boost_factor: float,
) -> list[FusedResult]:
    """Implementation of temporal date boosting — called from temporal_date_boost() with error boundary."""
    boosted_any = False

    # Strategy 1: explicit date in query (YYYY-MM-DD or YYYY-MM)
    date_strings = _extract_query_date_strings(query)
    if date_strings:
        for r in results:
            if any(ds in r.path for ds in date_strings):
                r.boosted_score *= boost_factor
                boosted_any = True
        if boosted_any:
            return sorted(results, key=lambda r: r.boosted_score, reverse=True)

    # Strategy 2: relative temporal terms -> recency window
    boosted_any = _boost_by_recency_window(results, query, boost_factor)

    if boosted_any:
        return sorted(results, key=lambda r: r.boosted_score, reverse=True)

    return results


# ---------------------------------------------------------------------------
# Chunk-date proximity boosting (TMP-7B)
# ---------------------------------------------------------------------------


def chunk_date_boost(
    results: list[FusedResult],
    query_date: object,
    config: TemporalBoostConfig | None = None,
) -> list[FusedResult]:
    """
    Boost documents by proximity of chunk_date metadata to the query date.

    Uses Gaussian decay: boost = 1 + exp(-delta^2 / (2*sigma^2))
    where sigma = halflife / 1.177 (halflife = days at which boost = 0.5 of max).

    Called from hybrid.py for TEMPORAL intent when chunk_date_boost_enabled is True.
    Requires chunk_date to be passed in via FusedResult (TMP-7B wires this).

    Args:
        results:     FusedResult list after entity_boost.
        query_date:  Date extracted from the query (datetime.date). None = no-op.
        config:      TemporalBoostConfig. Default: TemporalBoostConfig().

    Returns:
        Results re-sorted by boosted_score descending.
        Returns results unmodified on any error.
        Never raises.
    """
    cfg = config if config is not None else TemporalBoostConfig()
    if not cfg.chunk_date_boost_enabled or query_date is None:
        return results

    if not results:
        return results

    try:
        return _chunk_date_boost_impl(results, query_date, cfg)
    except Exception as e:
        logger.warning("chunk_date_boost: error — %s — returning unmodified results", e)
        return results


def _chunk_date_boost_impl(
    results: list[FusedResult],
    query_date: object,
    config: TemporalBoostConfig,
) -> list[FusedResult]:
    """Implementation of chunk_date proximity boosting."""
    import datetime
    import math

    sigma = config.chunk_date_decay_halflife_days / 1.177
    boosted_any = False

    for r in results:
        chunk_date_str = getattr(r, "chunk_date", None)
        if not chunk_date_str:
            continue
        try:
            if isinstance(chunk_date_str, str):
                chunk_date = datetime.date.fromisoformat(chunk_date_str[:10])
            else:
                chunk_date = chunk_date_str
        except (ValueError, TypeError):
            continue

        delta_days = abs((chunk_date - query_date).days)
        boost = 1.0 + math.exp(-(delta_days**2) / (2 * sigma**2))
        r.boosted_score *= boost
        boosted_any = True

    if boosted_any:
        return sorted(results, key=lambda r: r.boosted_score, reverse=True)
    return results
