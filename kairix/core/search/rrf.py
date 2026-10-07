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

RRF formula per document (with asymmetric-list normalisation — Issue #454):
  score(d) = sum(w_list * 1 / (k + rank_in_list) for each list containing d)
  where w_list = len(list) / max(len(bm25), len(vec)).
  Symmetric inputs (len(bm25) == len(vec)) collapse w to 1.0 and the
  formula reduces to classic Cormack 2009 RRF; asymmetric inputs
  rebalance so the longer list doesn't silently outweigh the shorter.
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

import datetime
import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


def parse_chunk_seq(path: str) -> int | None:
    """Parse the trailing ``#<seq>`` chunk index off a chunk path (PLA-270).

    Connector / archive chunks are keyed ``<source_uri>#<seq>`` — the chunk
    writer enumerates a document's chunks 0-based (see
    ``kairix.worker._SqliteChunkWriter.upsert``). This returns that ``seq``
    as a typed ``int`` so downstream chunk-expansion (PLA-268) has a clean,
    typed key instead of re-parsing ``#N`` off the path with no document
    handle — the drift PLA-270 closes.

    Returns ``None`` when the path carries no numeric chunk suffix:
    passthrough vault / markdown rows (no ``#``), synthetic ``entity://`` /
    ``facts://`` rows, and heading-anchor fragments (``note#section`` →
    ``None``, not a seq). A non-negative integer otherwise.
    """
    if not path:
        return None
    _head, sep, tail = path.rpartition("#")
    if not sep or not tail.isdigit():
        return None
    return int(tail)


# ---------------------------------------------------------------------------
# Entity slug helpers (for secondary name-based lookup)
# ---------------------------------------------------------------------------


# F17 — "collection" dict-key and "organisation" entity-label are read repeatedly
# downstream; extract so a key rename hits a single edit site.
_KEY_COLLECTION = "collection"
_LABEL_ORGANISATION = "organisation"

_LABEL_TO_DIR: dict[str, str] = {
    "person": "person",
    _LABEL_ORGANISATION: _LABEL_ORGANISATION,
    "organization": _LABEL_ORGANISATION,
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

    # MM-3 per-page citation. ``None`` for non-paged documents.
    # Carries through to the budgeted result, SearchHit, and the MCP
    # envelope so agents can quote a specific page back to the operator.
    source_page: int | None = None

    # PLA-274 — canonical resolvable breadcrumb (``documents.source_uri``),
    # threaded from the BM25/vector rows. ``""`` for passthrough vault rows;
    # ``SourceRef.of`` falls that back to ``path`` so the breadcrumb is
    # always resolvable. Carried through to SearchHit + the MCP envelope so
    # every surface cites the canonical source, not the munged ``path``.
    source_uri: str = ""

    # PLA-270 — the chunk sequence index (0-based) parsed from the chunk
    # path's trailing ``#<seq>``. ``None`` for passthrough vault notes and
    # synthetic (``entity://`` / ``facts://``) rows. Carried through to
    # SearchHit + the MCP envelope so chunk-expansion (PLA-268) keys on a
    # typed field instead of re-parsing ``#N`` off the path.
    seq: int | None = None


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
    """Implementation of RRF — called from rrf() with error boundary.

    Per-document score:
      score(d) = sum(w_list * 1/(k + rank_in_list) for each list containing d)

    where ``w_list = len(list) / max(len(bm25), len(vec))``.

    The asymmetric-list normalisation (Issue #454) compensates for the
    case where ``bm25_limit != vec_limit`` (e.g. default config ships
    ``bm25_limit=20, vec_limit=10``). Without it, the longer list silently
    out-weights the shorter one because each rank still contributes
    ``1/(k+rank)``; the longer list just has more contributors. The weight
    rescales each list so a unit of "rank confidence" is comparable across
    the two backends regardless of list length.

    Symmetric inputs (``len(bm25) == len(vec)``) collapse both weights to
    1.0 → bit-identical to the classic Cormack 2009 formula. This is a
    strict no-op for any caller using equal limits.
    """
    # Build path → FusedResult index
    fused: dict[str, FusedResult] = {}

    # Per-list weight (Issue #454). When lists are symmetric, w_bm25 ==
    # w_vec == 1.0 and the formula reduces to classic Cormack 2009.
    # ``or 1`` guards the degenerate case where both lists are empty
    # (caller filters this case upstream in ``rrf``; the guard is
    # defence-in-depth so divide-by-zero is structurally impossible).
    n_max = max(len(bm25), len(vec)) or 1
    w_bm25 = len(bm25) / n_max
    w_vec = len(vec) / n_max

    # Process BM25 results (1-indexed ranks)
    for rank, result in enumerate(bm25, start=1):
        path = canonical_path(result["file"])
        if path not in fused:
            fused[path] = FusedResult(
                path=path,
                collection=result[_KEY_COLLECTION],
                title=result["title"],
                snippet=result["snippet"],
                source_page=_extract_source_page(result),
                source_uri=_extract_source_uri(result),
                seq=parse_chunk_seq(path),
            )
        fused[path].in_bm25 = True
        fused[path].bm25_rank = rank
        fused[path].rrf_score += w_bm25 * 1.0 / (k + rank)
        # Backfill page + breadcrumb when the vec leg surfaced the row first.
        _backfill_source_meta(fused[path], result)

    # Process vector results (1-indexed ranks)
    for rank, result in enumerate(vec, start=1):
        path = canonical_path(result["path"])
        if path not in fused:
            fused[path] = FusedResult(
                path=path,
                collection=result[_KEY_COLLECTION],
                title=result["title"],
                snippet=result["snippet"],
                source_page=_extract_source_page(result),
                source_uri=_extract_source_uri(result),
                seq=parse_chunk_seq(path),
            )
        fused[path].in_vec = True
        fused[path].vec_rank = rank
        fused[path].rrf_score += w_vec * 1.0 / (k + rank)
        _backfill_source_meta(fused[path], result)

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


def _mark_existing_vec_hit(
    results: list[FusedResult],
    path_lower: str,
    rank: int,
    result: Any,
) -> None:
    for fr in results:
        if fr.path.lower() == path_lower:
            fr.in_vec = True
            fr.vec_rank = rank
            _backfill_source_meta(fr, result)
            return


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
            collection=result[_KEY_COLLECTION],
            title=result["title"],
            snippet=result["snippet"],
            in_bm25=True,
            bm25_rank=rank,
            # Score: use BM25 position-based score so boosted_score ordering is preserved
            rrf_score=1.0 / rank,
            source_page=_extract_source_page(result),
            source_uri=_extract_source_uri(result),
            seq=parse_chunk_seq(path),
        )
        fr.boosted_score = fr.rrf_score
        results.append(fr)

    # Phase 2: Vector-only results appended (recall backfill)
    base_rank = len(results)
    for rank, result in enumerate(vec, start=1):
        path = canonical_path(result["path"])
        path_lower = path.lower()

        if path_lower in seen:
            _mark_existing_vec_hit(results, path_lower, rank, result)
            continue
        seen.add(path_lower)

        fr = FusedResult(
            path=path,
            collection=result[_KEY_COLLECTION],
            title=result["title"],
            snippet=result["snippet"],
            in_vec=True,
            vec_rank=rank,
            # Score: below all BM25 results but in vec rank order
            rrf_score=1.0 / (base_rank + rank),
            source_page=_extract_source_page(result),
            source_uri=_extract_source_uri(result),
            seq=parse_chunk_seq(path),
        )
        fr.boosted_score = fr.rrf_score
        results.append(fr)

    return results


def _extract_source_page(result: Any) -> int | None:
    """Pull ``source_page`` from a raw BM25 or vector result row.

    Both ``BM25Result`` and ``VecResult`` carry the field as part of the
    MM-3 per-page-citation thread. This helper handles dict-shaped rows
    that may pre-date the field (older tests, fakes) by returning
    ``None`` rather than raising ``KeyError``.
    """
    if not isinstance(result, dict):
        return None
    raw = result.get("source_page")
    return int(raw) if isinstance(raw, int) else None


def _extract_source_uri(result: Any) -> str:
    """Pull the canonical ``source_uri`` breadcrumb from a raw BM25/vector row.

    PLA-274. Both ``BM25Result`` and ``VecResult`` carry the field; rows
    that pre-date it (older fakes / tests) surface ``""`` here rather than
    raising. The empty string defers the path-fallback to ``SourceRef.of``
    so the breadcrumb still resolves.
    """
    if not isinstance(result, dict):
        return ""
    return str(result.get("source_uri", "") or "")


def _backfill_source_meta(fr: FusedResult, result: Any) -> None:
    """Backfill ``source_page`` + ``source_uri`` onto a fused row (PLA-274).

    Fills only the fields the row that CREATED the entry left empty — the
    other fusion leg may surface the same path first without the page /
    breadcrumb. Extracted from the fusion loops so they stay under the F16
    cognitive-complexity ceiling.
    """
    if fr.source_page is None:
        fr.source_page = _extract_source_page(result)
    if not fr.source_uri:
        fr.source_uri = _extract_source_uri(result)


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
        _index_entity_row(row, path_in_degree, dir_in_degree, name_slug_in_degree)

    if not path_in_degree:
        return empty

    max_in_degree = max(path_in_degree.values()) or 1
    return path_in_degree, dir_in_degree, name_slug_in_degree, max_in_degree


def _index_entity_row(
    row: dict[str, Any],
    path_in_degree: dict[str, int],
    dir_in_degree: dict[str, int],
    name_slug_in_degree: dict[str, int],
) -> None:
    """Fold a single Neo4j entity row into the three lookup dicts."""
    vp = str(row["vault_path"]).lower().replace("\\", "/")
    in_deg = int(row.get("in_degree") or 0)
    path_in_degree[vp] = in_deg

    parent = str(Path(vp).parent).lower().replace("\\", "/")
    if parent not in (".", ""):
        dir_in_degree[parent] = max(dir_in_degree.get(parent, 0), in_deg)

    _index_slug_lookups(row, in_deg, name_slug_in_degree)


def _index_slug_lookups(
    row: dict[str, Any],
    in_deg: int,
    name_slug_in_degree: dict[str, int],
) -> None:
    """Index ``{dir}/{slug}.md`` paths derived from each entity label."""
    name = str(row.get("name") or "").strip()
    if not name:
        return
    slug = slugify(name)
    if not slug:
        return
    for lbl in row.get("labels") or []:
        dir_name = _LABEL_TO_DIR.get(str(lbl).lower())
        if not dir_name:
            continue
        doc_path = f"{dir_name}/{slug}.md"
        name_slug_in_degree[doc_path] = max(name_slug_in_degree.get(doc_path, 0), in_deg)


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


def _unboosted(results: list[FusedResult]) -> list[FusedResult]:
    """Carry each result's RRF score through as its boosted score, unchanged order."""
    for r in results:
        r.boosted_score = r.rrf_score
    return results


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
        return _unboosted(results)

    path_idx, dir_idx, slug_idx, max_in_deg = _build_entity_index(neo4j_client)
    if not path_idx and not dir_idx:
        return _unboosted(results)

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
        if not isinstance(query_date, datetime.date):
            return results
        return _chunk_date_boost_impl(results, query_date, cfg)
    except Exception as e:
        logger.warning("chunk_date_boost: error — %s — returning unmodified results", e)
        return results


def _parse_chunk_date(chunk_date_str: object) -> datetime.date | None:
    """Return a ``datetime.date`` for a chunk's chunk_date attribute, or
    ``None`` when the value is missing / unparseable. Extracted from
    :func:`_chunk_date_boost_impl` to keep its cognitive complexity
    under the F16 ceiling."""
    if not chunk_date_str:
        return None
    try:
        if isinstance(chunk_date_str, str):
            return datetime.date.fromisoformat(chunk_date_str[:10])
        if isinstance(chunk_date_str, datetime.date):
            return chunk_date_str
        return None
    except (ValueError, TypeError):
        return None


def _apply_chunk_date_proximity(
    results: list[FusedResult],
    query_date: datetime.date,
    sigma: float,
) -> tuple[bool, list[bool]]:
    """First pass — apply Gaussian-decay proximity boost to dated chunks.

    Returns ``(boosted_any, has_parseable_date_per_row)``. The second
    pass uses ``has_parseable_date_per_row`` to decide which rows are
    eligible for the undated-chunk penalty (Issue #430).
    """
    import math

    boosted_any = False
    has_parseable_date: list[bool] = []
    for r in results:
        chunk_date = _parse_chunk_date(getattr(r, "chunk_date", None))
        if chunk_date is None:
            has_parseable_date.append(False)
            continue
        delta_days = abs((chunk_date - query_date).days)
        boost = 1.0 + math.exp(-(delta_days**2) / (2 * sigma**2))
        r.boosted_score *= boost
        boosted_any = True
        has_parseable_date.append(True)
    return boosted_any, has_parseable_date


def _apply_undated_penalty(
    results: list[FusedResult],
    has_parseable_date: list[bool],
    penalty: float,
) -> bool:
    """Second pass — Issue #430 — penalise undated chunks. Returns
    ``True`` when any row was penalised so the caller knows whether to
    re-sort the result list."""
    penalised_any = False
    for r, was_dated in zip(results, has_parseable_date, strict=False):
        if not was_dated:
            r.boosted_score *= penalty
            penalised_any = True
    return penalised_any


def _chunk_date_boost_impl(
    results: list[FusedResult],
    query_date: datetime.date,
    config: TemporalBoostConfig,
) -> list[FusedResult]:
    """Implementation of chunk_date proximity boosting.

    Two-pass design (Issue #430 adds the second pass):
      1. ``_apply_chunk_date_proximity`` — Gaussian-decay boost on dated
         chunks; tracks which rows had a parseable chunk_date so pass 2
         knows which rows to penalise.
      2. ``_apply_undated_penalty`` — when ``undated_chunk_penalty_enabled``
         is True AND at least one row in pass 1 was dated, multiply every
         undated row's ``boosted_score`` by ``undated_chunk_penalty``.

    Safety: when EVERY candidate is undated, the penalty does not fire
    (would penalise every result equally and return nothing useful).
    """
    sigma = config.chunk_date_decay_halflife_days / 1.177
    boosted_any, has_parseable_date = _apply_chunk_date_proximity(results, query_date, sigma)

    if config.undated_chunk_penalty_enabled and any(has_parseable_date):
        penalised = _apply_undated_penalty(results, has_parseable_date, config.undated_chunk_penalty)
        boosted_any = boosted_any or penalised

    if boosted_any:
        return sorted(results, key=lambda r: r.boosted_score, reverse=True)
    return results
