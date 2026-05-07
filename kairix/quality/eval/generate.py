"""
GPL-inspired automated evaluation suite generation for kairix.

Implements the Generative Pseudo Labeling pipeline (Wang et al. 2022):

  1. sample_documents  — draw representative docs from the kairix SQLite index
  2. generate_queries  — prompt gpt-4o-mini to write queries the doc answers
  3. retrieve          — run hybrid_search for each generated query
  4. judge             — call judge.judge_batch() to grade retrieved docs
  5. build_case        — emit a BenchmarkCase with gold_titles (0/1/2 graded)
  6. enrich_suite      — convert an existing single-gold-path suite to graded

Reference:
  Wang et al. (2022). GPL: Generative Pseudo Labeling for Unsupervised Domain
  Adaptation of Dense Retrieval. NAACL 2022.
  https://arxiv.org/abs/2112.09118

Also provides enrich_suite() for converting existing BM25-biased suites to
title-based graded relevance without regenerating all queries from scratch.
"""

from __future__ import annotations

import json
import logging
import random
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from kairix.quality.eval.judge import (
    JUDGE_DEPLOYMENT,
    JudgeCalibrationError,
    JudgeResult,
    _call_llm,
    calibrate,
    fetch_llm_credentials,
    judge_batch,
)

logger = logging.getLogger(__name__)


# Path to kairix's SQLite index (resolved via kairix.core.db)
def _get_db_path_str() -> str:
    from kairix.core.db import get_db_path

    return str(get_db_path())


# Category target distribution for generate_suite()
_TARGET_DISTRIBUTION: dict[str, float] = {
    "recall": 0.40,
    "temporal": 0.15,
    "entity": 0.15,
    "conceptual": 0.12,
    "multi_hop": 0.10,
    "procedural": 0.08,
}

# Minimum document body length to sample (chars)
_MIN_DOC_LENGTH: int = 200

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class GeneratedQuery:
    """A query generated from a source document."""

    query: str
    intent: str  # recall | temporal | entity | conceptual | multi_hop | procedural
    source_doc_path: str
    source_doc_title: str


@dataclass
class GenerationResult:
    """Result of a generate_suite() or enrich_suite() run."""

    output_path: str
    n_generated: int
    n_accepted: int
    n_rejected: int  # no grade-2 doc found
    n_failed: int  # API or retrieval error
    category_counts: dict[str, int]
    calibration_passed: bool
    errors: list[str] = field(default_factory=list)


@dataclass
class EnrichmentResult:
    """Result of an enrich_suite() run."""

    output_path: str
    n_cases: int
    n_enriched: int  # cases that received gold_titles
    n_skipped: int  # cases where no grade-1+ doc was found (kept with existing gold)
    n_failed: int  # cases where retrieval/judge failed
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Document sampling helpers (extracted to reduce cognitive complexity)
# ---------------------------------------------------------------------------


def query_documents_from_db(
    db_path: str,
    collections: list[str] | None,
    min_length: int,
    n: int,
) -> list[Any]:
    """Execute SQL to fetch candidate documents from the kairix SQLite index.

    Returns a list of sqlite3.Row objects, or [] on any failure.
    """
    try:
        from kairix.core.db import open_db

        db = open_db(Path(db_path))
        db.row_factory = sqlite3.Row
    except Exception as e:
        logger.warning("sample_documents: failed to open %r — %s", db_path, e)
        return []

    try:
        if collections:
            placeholders = ",".join("?" * len(collections))
            rows = db.execute(
                f"""
                SELECT d.path, d.title, d.collection, c.doc
                FROM documents d
                JOIN content c ON c.hash = d.hash
                WHERE d.collection IN ({placeholders})
                  AND lower(d.path) NOT LIKE '%archive%'
                  AND length(c.doc) >= ?
                ORDER BY RANDOM()
                LIMIT ?
                """,
                (*collections, min_length, n * 3),
            ).fetchall()
        else:
            rows = db.execute(
                """
                SELECT d.path, d.title, d.collection, c.doc
                FROM documents d
                JOIN content c ON c.hash = d.hash
                WHERE lower(d.path) NOT LIKE '%archive%'
                  AND length(c.doc) >= ?
                ORDER BY RANDOM()
                LIMIT ?
                """,
                (min_length, n * 3),
            ).fetchall()
        db.close()
        return rows
    except Exception as e:
        logger.warning("sample_documents: query error — %s", e)
        try:
            db.close()
        except Exception:  # noqa: S110
            pass
        return []


def filter_and_process_sampled_rows(
    rows: list[Any],
    min_length: int,
) -> list[dict[str, Any]]:
    """Process raw DB rows: strip YAML frontmatter, filter by length, build doc dicts."""
    docs = []
    for row in rows:
        body = row["doc"] or ""
        if body.startswith("---"):
            parts = body.split("---", 2)
            body = parts[2].strip() if len(parts) >= 3 else body
        if len(body) < min_length:
            continue
        docs.append(
            {
                "path": row["path"],
                "title": str(row["title"] or Path(row["path"]).stem),
                "collection": row["collection"],
                "body": body[:2000],
            }
        )
    return docs


# ---------------------------------------------------------------------------
# Document sampling
# ---------------------------------------------------------------------------


def sample_documents(
    db_path: str = _get_db_path_str(),
    n: int = 200,
    collections: list[str] | None = None,
    seed: int | None = None,
) -> list[dict[str, Any]]:
    """
    Sample documents from the kairix SQLite index.

    Proportionally samples across collections, skipping archived docs and
    very short documents (< _MIN_DOC_LENGTH chars).

    Args:
        db_path:     Path to kairix SQLite database.
        n:           Target number of documents to sample.
        collections: Restrict to these collection names (None = all).
        seed:        Random seed for reproducibility.

    Returns:
        List of dicts with keys: path, title, collection, body (truncated to 2000 chars).
    """
    if seed is not None:
        random.seed(seed)

    rows = query_documents_from_db(db_path, collections, _MIN_DOC_LENGTH, n)
    if not rows:
        return []

    docs = filter_and_process_sampled_rows(rows, _MIN_DOC_LENGTH)
    # NOSONAR(python:S2245): non-security shuffle for benchmark sample
    # ordering — repeatable via random.seed() in tests; no trust boundary.
    random.shuffle(docs)
    return docs[:n]


# ---------------------------------------------------------------------------
# Query generation helpers (extracted to reduce cognitive complexity)
# ---------------------------------------------------------------------------


def build_generation_prompt(title: str, body: str, n: int, cats: list[str]) -> str:
    """Construct the LLM prompt for query generation."""
    cats_str = ", ".join(cats)
    snippet = body[:1000].replace("\n", " ")
    return (
        f"You are generating retrieval queries for an information retrieval benchmark.\n\n"
        f"Document title: {title}\n"
        f"Document content (excerpt): {snippet}\n\n"
        f"Write exactly {n} queries that this document would be the primary answer for.\n"
        f"Each query should:\n"
        f"  - Be a natural question or search phrase a user would actually type\n"
        f"  - Be specific enough that this document clearly answers it\n"
        f"  - Cover different aspects of the document's content (not just paraphrasing the title)\n\n"
        f"Label each query with its intent type from: {cats_str}\n\n"
        f"Reply ONLY with JSON array:\n"
        f'[{{"query": "...", "intent": "recall"}}, ...]\n'
        f"No explanation, no markdown, just the JSON array."
    )


def parse_llm_query_response(
    content: str,
    allowed_cats: list[str],
    source_path: str,
    title: str,
) -> list[GeneratedQuery]:
    """Extract JSON from LLM response and validate into GeneratedQuery objects."""
    arr_match = re.search(r"\[.*\]", content, re.DOTALL)
    if not arr_match:
        raise ValueError(f"No JSON array in response: {content[:200]!r}")
    raw = json.loads(arr_match.group())
    queries = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        q = str(item.get("query", "")).strip()
        intent = str(item.get("intent", "recall")).strip().lower()
        if not q:
            continue
        if intent not in allowed_cats:
            intent = "recall"
        queries.append(
            GeneratedQuery(
                query=q,
                intent=intent,
                source_doc_path=source_path,
                source_doc_title=title,
            )
        )
    return queries


# ---------------------------------------------------------------------------
# Query generation
# ---------------------------------------------------------------------------


def generate_queries(
    doc_title: str,
    doc_body: str,
    n: int = 2,
    categories: list[str] | None = None,
    api_key: str = "",
    endpoint: str = "",
    deployment: str = "gpt-4o-mini",
    source_doc_path: str = "",
    llm_fn: Callable[[str, str, str, str], str] | None = None,
) -> list[GeneratedQuery]:
    """
    Generate n retrieval queries that the given document would primarily answer.

    Prompts gpt-4o-mini to write queries that:
    - Would rank this document at position 1 in a well-functioning retrieval system
    - Cover diverse aspects of the document's content
    - Are labelled with the appropriate intent category

    Args:
        doc_title:       Document title (used as identifier).
        doc_body:        Document body text (first 1000 chars used in prompt).
        n:               Number of queries to generate (default: 2).
        categories:      Allowed intent categories (None = all standard categories).
        api_key:         Azure OpenAI API key.
        endpoint:        Azure OpenAI endpoint URL.
        deployment:      Model deployment name.
        source_doc_path: Original path of the document.

    Returns:
        List of GeneratedQuery. Returns [] on any failure (no raise).
    """
    allowed_cats = categories or list(_TARGET_DISTRIBUTION.keys())
    prompt = build_generation_prompt(doc_title, doc_body, n, allowed_cats)
    _llm = llm_fn or _call_llm

    for attempt in range(2):
        try:
            if not api_key or not endpoint:
                raise ValueError("No API credentials")
            content = _llm(prompt, api_key, endpoint, deployment)
            return parse_llm_query_response(content, allowed_cats, source_doc_path, doc_title)
        except Exception as e:
            if attempt == 0:
                logger.debug(
                    "generate_queries: parse failure (attempt 1) for %r — %s",
                    doc_title,
                    e,
                )
            else:
                logger.warning(
                    "generate_queries: failed for %r after 2 attempts — %s",
                    doc_title,
                    e,
                )

    return []


# ---------------------------------------------------------------------------
# Retrieval — delegates to shared retrieval module
# ---------------------------------------------------------------------------


def _retrieve(query: str, intent: str, agent: str = "shape") -> tuple[list[str], list[str]]:
    """
    Run hybrid search and return (paths, snippets).
    Returns ([], []) on any failure.
    """
    try:
        from kairix.quality.eval.retrieval import retrieve

        result = retrieve(query=query, system="hybrid", agent=agent)
        # Truncate snippets to 300 chars for judge input
        snippets = [s[:300] for s in result.snippets]
        return result.paths, snippets
    except Exception as e:
        logger.warning("_retrieve: error for query %r — %s", query[:60], e)
        return [], []


# ---------------------------------------------------------------------------
# Case builder
# ---------------------------------------------------------------------------


def build_case(
    query: str,
    intent: str,
    judge_result: JudgeResult,
    paths: list[str],
    snippets: list[str],
    case_id: str,
) -> dict[str, Any] | None:
    """
    Build a benchmark case dict from judge results.

    Accepts only if at least one document received grade 2. The gold_titles
    list includes all documents with grade >= 1.

    Args:
        query:        The search query.
        intent:       The intent category.
        judge_result: Output of judge_batch().
        paths:        Retrieved paths (parallel to judge candidates).
        snippets:     Retrieved snippets.
        case_id:      Case identifier (e.g. "GEN-R001").

    Returns:
        Dict ready for YAML serialisation, or None if no grade-2 doc found.
    """

    grade_2_count = sum(1 for g in judge_result.grades.values() if g == 2)
    if grade_2_count == 0:
        return None

    # Build gold_titles from grades
    gold_titles: list[dict[str, Any]] = []
    for stem, grade in judge_result.grades.items():
        if grade >= 1:
            gold_titles.append({"title": stem, "relevance": grade})

    # Sort by relevance desc for readability
    gold_titles.sort(key=lambda x: -int(x["relevance"]))

    return {
        "id": case_id,
        "category": intent,
        "query": query,
        "score_method": "ndcg",
        "gold_titles": gold_titles,
    }


# ---------------------------------------------------------------------------
# Suite generation helpers (extracted to reduce cognitive complexity)
# ---------------------------------------------------------------------------


def _empty_generation_result(
    output_path: str,
    calibration_passed: bool,
    errors: list[str],
) -> GenerationResult:
    """Build a GenerationResult with zero counts (early-exit helper)."""
    return GenerationResult(
        output_path=output_path,
        n_generated=0,
        n_accepted=0,
        n_rejected=0,
        n_failed=0,
        category_counts={},
        calibration_passed=calibration_passed,
        errors=errors,
    )


def resolve_credentials(
    api_key: str | None,
    endpoint: str | None,
    deployment: str,
) -> tuple[str, str, str]:
    """Fetch credentials from env/Key Vault when not provided.

    Caller-wins semantic:
      - If ``api_key`` is None or empty, use the fetched key.
      - If ``endpoint`` is None or empty, use the fetched endpoint.
      - If ``deployment`` equals the default sentinel ``JUDGE_DEPLOYMENT``
        and the vault has a different non-empty value, use the vault's.
        Otherwise the caller's deployment wins.

    The original logic (``fetched_dep != "gpt-4o-mini" or deployment == "gpt-4o-mini"``)
    overrode the caller whenever the vault returned anything non-default —
    inverting the intuitive "caller wins unless they didn't care" semantic.
    Bug fix from #143 Phase 0.

    Returns (api_key, endpoint, deployment). Raises on credential-fetch failure.
    """
    fetched_key, fetched_ep, fetched_dep = fetch_llm_credentials()
    api_key = api_key or fetched_key
    endpoint = endpoint or fetched_ep
    # Caller's deployment wins unless they passed the default sentinel and
    # the vault offers a non-default override.
    if deployment == JUDGE_DEPLOYMENT and fetched_dep and fetched_dep != JUDGE_DEPLOYMENT:
        deployment = fetched_dep
    return api_key, endpoint, deployment


def process_sampled_docs(
    docs: list[dict[str, Any]],
    n_cases: int,
    active_cats: list[str],
    api_key: str,
    endpoint: str,
    deployment: str,
    agent: str,
    query_fn: Callable[..., list[GeneratedQuery]] | None,
    retrieve_fn: Callable[..., tuple[list[str], list[str]]] | None,
    judge_fn: Callable[..., JudgeResult] | None,
) -> tuple[list[dict[str, Any]], int, int, dict[str, int]]:
    """Process sampled docs through the GPL pipeline (generate, retrieve, judge).

    Returns (accepted_cases, n_rejected, n_failed, category_counts).
    """
    accepted_cases: list[dict[str, Any]] = []
    n_rejected = 0
    n_failed = 0
    category_counts: dict[str, int] = {cat: 0 for cat in active_cats}
    id_counters: dict[str, int] = {cat: 0 for cat in active_cats}
    _cat_prefix = {
        "recall": "GEN-R",
        "temporal": "GEN-T",
        "entity": "GEN-E",
        "conceptual": "GEN-C",
        "multi_hop": "GEN-M",
        "procedural": "GEN-P",
    }

    for doc in docs:
        if len(accepted_cases) >= n_cases:
            break

        _gen_queries = query_fn or generate_queries
        queries = _gen_queries(
            doc_title=doc["title"],
            doc_body=doc["body"],
            n=2,
            categories=active_cats,
            api_key=api_key,
            endpoint=endpoint,
            deployment=deployment,
            source_doc_path=doc["path"],
        )

        for gq in queries:
            if len(accepted_cases) >= n_cases:
                break

            _retr = retrieve_fn or _retrieve
            paths, snippets = _retr(gq.query, gq.intent, agent=agent)
            if not paths:
                n_failed += 1
                continue

            candidates = list(
                zip(
                    [Path(p).stem for p in paths[:10]],
                    [s[:300] for s in snippets[:10]],
                    strict=False,
                )
            )

            _judge = judge_fn or judge_batch
            result = _judge(
                query=gq.query,
                candidates=candidates,
                api_key=api_key,
                endpoint=endpoint,
                deployment=deployment,
            )

            id_counters[gq.intent] = id_counters.get(gq.intent, 0) + 1
            prefix = _cat_prefix.get(gq.intent, "GEN-X")
            case_id = f"{prefix}{id_counters[gq.intent]:03d}"

            case = build_case(
                query=gq.query,
                intent=gq.intent,
                judge_result=result,
                paths=paths,
                snippets=snippets,
                case_id=case_id,
            )
            if case is None:
                n_rejected += 1
                continue

            accepted_cases.append(case)
            category_counts[gq.intent] = category_counts.get(gq.intent, 0) + 1

    return accepted_cases, n_rejected, n_failed, category_counts


def write_generated_suite(
    output_path: str,
    cases: list[dict[str, Any]],
    cats: list[str],
    errors: list[str],
) -> None:
    """Create YAML output file for the generated suite."""
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    suite_doc = {
        "meta": {
            "version": "1.0",
            "generated_by": "kairix eval generate",
            "n_cases": len(cases),
            "categories": cats,
            "score_method": "ndcg",
        },
        "cases": cases,
    }

    try:
        with output.open("w", encoding="utf-8") as f:
            yaml.dump(
                suite_doc,
                f,
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
            )
    except Exception as e:
        errors.append(f"Failed to write {output_path}: {e}")


# ---------------------------------------------------------------------------
# Suite generation
# ---------------------------------------------------------------------------


def generate_suite(
    db_path: str = _get_db_path_str(),
    output_path: str = "suites/generated.yaml",
    n_cases: int = 100,
    categories: list[str] | None = None,
    api_key: str | None = None,
    endpoint: str | None = None,
    deployment: str = "gpt-4o-mini",
    calibrate_first: bool = True,
    seed: int | None = None,
    agent: str = "shape",
    collections: list[str] | None = None,
    sample_fn: Callable[..., list[dict[str, Any]]] | None = None,
    query_fn: Callable[..., list[GeneratedQuery]] | None = None,
    retrieve_fn: Callable[..., tuple[list[str], list[str]]] | None = None,
    judge_fn: Callable[..., JudgeResult] | None = None,
) -> GenerationResult:
    """
    Generate a benchmark suite using the GPL pipeline.

    Pipeline: sample docs → generate queries → hybrid retrieve → LLM judge → YAML.

    Args:
        db_path:        Path to kairix SQLite database.
        output_path:    Output YAML file path.
        n_cases:        Target number of accepted cases.
        categories:     Categories to include (None = all). Controls doc sampling
                        and query generation intent labels.
        api_key:        Azure OpenAI API key (None = auto-fetch from env/Key Vault).
        endpoint:       Azure OpenAI endpoint URL (None = auto-fetch).
        deployment:     Model deployment name.
        calibrate_first: Run calibration anchors before generation (default: True).
        seed:           Random seed for reproducibility.
        agent:          Agent name for hybrid search scoping.

    Returns:
        GenerationResult. Never raises — returns partial results on failure.
    """
    errors: list[str] = []

    # Credential resolution
    if api_key is None or endpoint is None:
        try:
            api_key, endpoint, deployment = resolve_credentials(api_key, endpoint, deployment)
        except Exception as e:
            errors.append(f"Failed to fetch credentials: {e}")
            return _empty_generation_result(output_path, False, errors)

    calibration_passed = False
    if calibrate_first:
        try:
            calibration_passed = calibrate(api_key, endpoint, deployment)
        except JudgeCalibrationError as e:
            errors.append(f"Calibration failed: {e}")
            return _empty_generation_result(output_path, False, errors)
    else:
        calibration_passed = True

    active_cats = categories or list(_TARGET_DISTRIBUTION.keys())

    # Sample documents — oversample to allow for rejection
    _sample = sample_fn or sample_documents
    docs = _sample(db_path=db_path, n=n_cases * 10, collections=collections, seed=seed)
    if not docs:
        errors.append("sample_documents: no documents returned — check db_path")
        return _empty_generation_result(output_path, calibration_passed, errors)

    accepted_cases, n_rejected, n_failed, category_counts = process_sampled_docs(
        docs,
        n_cases,
        active_cats,
        api_key,
        endpoint,
        deployment,
        agent,
        query_fn,
        retrieve_fn,
        judge_fn,
    )
    n_generated = len(accepted_cases) + n_rejected + n_failed

    write_generated_suite(output_path, accepted_cases, active_cats, errors)

    return GenerationResult(
        output_path=output_path,
        n_generated=n_generated,
        n_accepted=len(accepted_cases),
        n_rejected=n_rejected,
        n_failed=n_failed,
        category_counts=category_counts,
        calibration_passed=calibration_passed,
        errors=errors,
    )


# ---------------------------------------------------------------------------
# Suite enrichment helpers (extracted to reduce cognitive complexity)
# ---------------------------------------------------------------------------


def enrich_single_case(
    case: dict[str, Any],
    query: str,
    api_key: str,
    endpoint: str,
    deployment: str,
    agent: str,
    retrieve_fn: Callable[..., tuple[list[str], list[str]]] | None,
    judge_fn: Callable[..., JudgeResult] | None,
) -> tuple[dict[str, Any], str]:
    """Process one case for enrichment.

    Returns (updated_case, status) where status is 'enriched', 'skipped', or 'failed'.
    """
    _retr = retrieve_fn or _retrieve
    paths, snippets = _retr(query, case.get("category", "recall"), agent=agent)
    if not paths:
        return case, "failed"

    candidates = list(
        zip(
            [Path(p).stem for p in paths[:10]],
            [s[:300] for s in snippets[:10]],
            strict=False,
        )
    )

    _judge = judge_fn or judge_batch
    result = _judge(
        query=query,
        candidates=candidates,
        api_key=api_key,
        endpoint=endpoint,
        deployment=deployment,
    )

    has_relevant = any(g >= 1 for g in result.grades.values())
    if not has_relevant:
        return case, "skipped"

    gold_titles: list[dict[str, Any]] = [
        {"title": stem, "relevance": grade} for stem, grade in result.grades.items() if grade >= 1
    ]
    gold_titles.sort(key=lambda x: -int(x["relevance"]))

    updated = dict(case)
    updated["gold_titles"] = gold_titles
    updated["score_method"] = "ndcg"
    updated.pop("gold_paths", None)
    return updated, "enriched"


# ---------------------------------------------------------------------------
# Suite enrichment
# ---------------------------------------------------------------------------


def enrich_suite(
    suite_path: str,
    output_path: str,
    db_path: str = _get_db_path_str(),
    api_key: str | None = None,
    endpoint: str | None = None,
    deployment: str = "gpt-4o-mini",
    agent: str = "shape",
    retrieve_fn: Callable[..., tuple[list[str], list[str]]] | None = None,
    judge_fn: Callable[..., JudgeResult] | None = None,
) -> EnrichmentResult:
    """
    Enrich an existing suite's cases with graded gold_titles.

    For each case in the input suite:
    1. Run hybrid_search for the case's query
    2. Judge the top-10 retrieved docs with gpt-4o-mini
    3. Replace gold_path with gold_titles (graded 0/1/2)
    4. Preserve all other case fields unchanged

    Cases where no grade-1+ doc is found retain their existing gold information.

    Args:
        suite_path:  Input suite YAML path.
        output_path: Output YAML path (may equal suite_path for in-place update).
        db_path:     kairix SQLite path (not used directly; hybrid_search handles DB).
        api_key:     Azure OpenAI API key (None = auto-fetch).
        endpoint:    Azure OpenAI endpoint URL (None = auto-fetch).
        deployment:  Model deployment name.
        agent:       Agent name for hybrid search scoping.

    Returns:
        EnrichmentResult. Never raises.
    """
    errors: list[str] = []

    # Credential resolution — wrap in try/except so a credential-fetch failure
    # is surfaced via the result.errors list rather than propagating as an
    # uncaught exception. enrich_suite is documented "never raises"; mirrors
    # the same shape generate_suite already uses (#143 Phase 0).
    if api_key is None or endpoint is None:
        try:
            api_key, endpoint, deployment = resolve_credentials(api_key, endpoint, deployment)
        except Exception as e:
            errors.append(f"Failed to fetch credentials: {e}")
            return EnrichmentResult(
                output_path=output_path,
                n_cases=0,
                n_enriched=0,
                n_skipped=0,
                n_failed=0,
                errors=errors,
            )

    # Load input suite as raw YAML (preserve all fields)
    try:
        with open(suite_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except Exception as e:
        errors.append(f"Failed to load {suite_path}: {e}")
        return EnrichmentResult(
            output_path=output_path,
            n_cases=0,
            n_enriched=0,
            n_skipped=0,
            n_failed=0,
            errors=errors,
        )

    raw_cases: list[dict[str, Any]] = raw.get("cases", [])
    n_enriched = 0
    n_skipped = 0
    n_failed = 0

    enriched_cases = []
    for case in raw_cases:
        query = case.get("query", "")
        if not query:
            enriched_cases.append(case)
            n_skipped += 1
            continue

        updated_case, status = enrich_single_case(
            case,
            query,
            api_key,
            endpoint,
            deployment,
            agent,
            retrieve_fn,
            judge_fn,
        )
        enriched_cases.append(updated_case)
        if status == "enriched":
            n_enriched += 1
        elif status == "failed":
            n_failed += 1
        else:
            n_skipped += 1

    # Write output
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    out_doc = dict(raw)
    out_doc["cases"] = enriched_cases

    try:
        with output.open("w", encoding="utf-8") as f:
            yaml.dump(
                out_doc,
                f,
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
            )
    except Exception as e:
        errors.append(f"Failed to write {output_path}: {e}")

    return EnrichmentResult(
        output_path=output_path,
        n_cases=len(raw_cases),
        n_enriched=n_enriched,
        n_skipped=n_skipped,
        n_failed=n_failed,
        errors=errors,
    )
