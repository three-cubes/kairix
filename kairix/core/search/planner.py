"""
Multi-hop query planning for the kairix hybrid search pipeline.

Decomposes complex queries into 2-3 focused sub-queries via GPT-4o-mini
(using the existing kairix._azure.chat_completion — no extra dependencies),
runs them in parallel via ThreadPoolExecutor, and merges results with
Reciprocal Rank Fusion (RRF).

Phase 4B-2 — 2026-04-05
"""

from __future__ import annotations

import json
import logging
import re as _re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from kairix.core.search.rrf import RRF_K

logger = logging.getLogger(__name__)


def _find_query_entities(query: str, client: object) -> list[dict]:
    """Find entities mentioned in query words via Neo4j name search.

    Returns up to 6 unique entity dicts. Never raises.
    """
    words = [w.strip(".,;:?!\"'") for w in query.split() if len(w.strip(".,;:?!\"'")) > 3]
    found: list[dict] = []
    seen_ids: set[str] = set()
    for word in words[:6]:
        try:
            matches = client.find_by_name(word)
            for m in matches[:2]:
                if m.get("id") and m["id"] not in seen_ids:
                    seen_ids.add(m["id"])
                    found.append(m)
        except Exception:  # broad catch justified: Neo4j driver can raise arbitrary exceptions
            logger.debug("planner: Neo4j find_by_name failed for word %r", word)
    return found


def _build_entity_relationships(entities: list[dict], client: object) -> list[str]:
    """Build relationship lines for found entities.

    Returns list of formatted relationship strings. Never raises.
    """
    lines: list[str] = []
    for entity in entities[:3]:
        eid = entity.get("id")
        ename = entity.get("name", eid)
        if not eid:  # pragma: no cover — defensive; ``_find_query_entities`` already filters entities without an ``id`` so this branch is unreachable in practice
            continue
        try:
            related = client.related_entities(eid, max_hops=1)
            rel_names = [r.get("name") for r in related[:4] if r.get("name") and r.get("name") != ename]
            if rel_names:
                lines.append(f"- {ename} → {', '.join(rel_names)}")
        except Exception:  # broad catch justified: Neo4j driver can raise arbitrary exceptions
            logger.debug("planner: Neo4j related_entities failed for entity %r", eid)
    return lines


def neo4j_graph_context(query: str, client: object) -> str | None:
    """
    Build an entity relationship context string from Neo4j for use in the
    decompose LLM prompt. Finds entities mentioned in the query and returns
    their direct relationships as a short text block.

    Returns None if no relevant entities are found.
    """
    found_entities = _find_query_entities(query, client)
    if not found_entities:
        return None

    rel_lines = _build_entity_relationships(found_entities, client)
    if not rel_lines:
        return None

    return "Known entities related to this query:\n" + "\n".join(rel_lines)


_DECOMPOSE_PROMPT = (
    "You decompose complex queries into 2-3 focused sub-queries for document retrieval. "
    "Reply with ONLY a JSON array of strings, no prose.\n\n"
    "Query: {query}\n\n"
    "Rules:\n"
    "- Each sub-query should retrieve a distinct aspect needed to answer the original.\n"
    "- Maximum 3 sub-queries.\n"
    '- If the query is simple (single topic), return just ["original_query"].\n'
    "- Keep sub-queries concise (under 15 words each).\n\n"
    "Examples:\n"
    'Query: "how does OpenTelemetry tracing support twelve-factor app logging"\n'
    '["OpenTelemetry distributed tracing instrumentation", '
    '"twelve-factor app logging practices"]\n\n'
    'Query: "compare dbt testing strategy with code review best practices"\n'
    '["dbt testing strategy and methodology", '
    '"code review best practices and standards"]\n\n'
    'Query: "what is the relationship between entity graphs and retrieval quality"\n'
    '["entity graph construction and knowledge representation", '
    '"how entity awareness improves search retrieval"]'
)

_DECOMPOSE_PROMPT_WITH_CONTEXT = (
    "You decompose complex queries into 2-3 focused sub-queries for document retrieval. "
    "Reply with ONLY a JSON array of strings, no prose.\n\n"
    "{entity_context}\n\n"
    "Query: {query}\n\n"
    "Rules:\n"
    "- Each sub-query should retrieve a distinct aspect needed to answer the original.\n"
    "- Use entity relationships above to expand abbreviations or implied connections.\n"
    "- Maximum 3 sub-queries.\n"
    '- If the query is simple (single topic), return just ["original_query"].\n'
    "- Keep sub-queries concise (under 15 words each).\n\n"
    "Examples:\n"
    'Query: "how does OpenTelemetry tracing support twelve-factor app logging"\n'
    '["OpenTelemetry distributed tracing instrumentation", '
    '"twelve-factor app logging practices"]\n\n'
    'Query: "compare dbt testing strategy with code review best practices"\n'
    '["dbt testing strategy and methodology", '
    '"code review best practices and standards"]\n\n'
    'Query: "what is the relationship between entity graphs and retrieval quality"\n'
    '["entity graph construction and knowledge representation", '
    '"how entity awareness improves search retrieval"]'
)


class QueryPlanner:
    """LLM-based query decomposition with parallel execution and RRF merge.

    Uses kairix._azure.chat_completion — same Azure OpenAI endpoint as
    embeddings, no extra SDK dependencies.
    """

    def decompose(self, query: str, neo4j_client: object | None = None, llm_backend=None) -> list[str]:
        """
        Decompose a complex query into 2-3 focused sub-queries.

        Uses chat_completion (GPT-4o-mini via Azure AI Foundry) with a JSON-array
        prompt for reliable parsing. Falls back to [query] on any failure.
        """
        try:
            if (
                llm_backend is None
            ):  # pragma: no cover — production-only lazy default backend; tests inject FakeLLMBackend
                from kairix.platform.llm import get_default_backend as _get_llm

                llm_backend = _get_llm()
            chat_completion = llm_backend.chat
            # Inject entity graph context when available
            ctx = None
            if neo4j_client is not None and getattr(neo4j_client, "available", False):
                try:
                    ctx = neo4j_graph_context(query, neo4j_client)
                except Exception:  # pragma: no cover — defensive; ``neo4j_graph_context``'s helpers already catch driver exceptions, so this outer except is reachable only if those helpers themselves raise (currently impossible)
                    logger.debug("planner: Neo4j graph context unavailable")
            if ctx:
                prompt = _DECOMPOSE_PROMPT_WITH_CONTEXT.format(entity_context=ctx, query=query)
                logger.debug("planner: injecting entity context into decompose prompt")
            else:
                prompt = _DECOMPOSE_PROMPT.format(query=query)
            response = chat_completion(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=150,
            )
            if not response:
                logger.warning("planner: chat_completion returned empty response")
                return [query]

            subs = json.loads(response.strip())
            if isinstance(subs, list) and 1 <= len(subs) <= 3:
                subs = [s for s in subs if isinstance(s, str) and s.strip()]
                if subs:
                    logger.debug("planner: decomposed into %d sub-queries", len(subs))
                    return subs
        except json.JSONDecodeError as _e:
            logger.warning("planner: JSON parse failed (%s) — trying regex fallback", _e)
            # Regex fallback: extract quoted strings (min 5 chars) from LLM output
            if response:
                matches = _re.findall(r'"([^"]{5,})"', response)
                if matches:
                    logger.debug("planner: regex fallback extracted %d sub-queries", len(matches))
                    return matches[:3]
        except Exception as _e:
            logger.warning("planner: decompose failed (%s) — falling back to original query", _e)
        return [query]

    @staticmethod
    def _result_key(r: Any) -> str:
        """Extract a deduplication key (path) from a heterogeneous result object."""
        if isinstance(r, dict):
            return r.get("file") or r.get("path") or str(r)
        if hasattr(r, "result") and hasattr(r.result, "path"):
            return r.result.path or str(r)
        return getattr(r, "path", None) or str(r)

    def retrieve_and_merge(
        self,
        sub_queries: list[str],
        search_fn: Callable[[str], list[Any]],
        top_k_per_sub: int = 5,
        final_top_k: int = 6,
    ) -> list[Any]:
        """
        Run search_fn for each sub-query in parallel, deduplicate by path,
        and merge with RRF. Returns top final_top_k results.

        Args:
            sub_queries:   List of sub-queries from decompose().
            search_fn:     Callable(query) -> list[result]; each result must
                           have a .path attribute.
            top_k_per_sub: Number of results to retrieve per sub-query.
            final_top_k:   Number of final merged results to return.
        """
        all_results: dict[str, Any] = {}
        rank_lists: list[list[str]] = []

        with ThreadPoolExecutor(max_workers=min(len(sub_queries), 3)) as pool:
            futures = {pool.submit(search_fn, q): q for q in sub_queries}
            for future in as_completed(futures):
                try:
                    results = future.result() or []
                except Exception as _e:
                    logger.warning("planner: sub-query future failed — %s", _e)
                    results = []

                rank_list: list[str] = []
                for r in results[:top_k_per_sub]:
                    key = self._result_key(r)
                    if key not in all_results:
                        all_results[key] = r
                    rank_list.append(key)
                rank_lists.append(rank_list)

        # Reciprocal Rank Fusion
        rrf_scores: dict[str, float] = {}
        for rank_list in rank_lists:
            for rank, key in enumerate(rank_list):
                rrf_scores[key] = rrf_scores.get(key, 0.0) + 1.0 / (RRF_K + rank + 1)

        ranked_keys = sorted(rrf_scores, key=lambda k: rrf_scores[k], reverse=True)
        merged = [all_results[k] for k in ranked_keys[:final_top_k] if k in all_results]
        logger.debug(
            "planner: merged %d results from %d sub-queries",
            len(merged),
            len(sub_queries),
        )
        return merged
