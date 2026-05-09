"""
kairix.agents.mcp.server — MCP server exposing kairix tools to MCP-compatible agents.

Provides six tools:
  search       Search your knowledge store — finds the best answers to any question
  entity       Entity lookup from Neo4j
  prep         Context preparation: tiered L0/L1 summary generation
  timeline     Temporal query rewriting + date-aware retrieval
  contradict   Check new content against existing knowledge for contradictions
  usage_guide  Return the kairix agent usage guide (self-documentation)

The server uses FastMCP (from the ``mcp`` package). Install via:
    pip install kairix[agents]

Tool functions are pure Python functions importable without FastMCP installed —
import them directly for unit testing or programmatic use.

Design principles:
  - Never raises; returns error dicts on failure so agents can handle gracefully
  - All inputs/outputs are JSON-serialisable primitives
  - Dependencies initialised lazily on first call
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import requests

from kairix.agents.mcp.errors import wrap_tool_errors
from kairix.core.search.intent import QueryIntent
from kairix.core.search.scope import Scope
from kairix.text import estimate_tokens

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_SCOPE: Scope = Scope.SHARED_AGENT

# ---------------------------------------------------------------------------
# Budget inference + entity name extraction (AFF-1, AFF-3)
# ---------------------------------------------------------------------------

_RESEARCH_WORDS = re.compile(r"\b(research|compare|analyse|analyze|comprehensive|detailed)\b", re.IGNORECASE)

_ENTITY_PREFIX_RE = re.compile(
    r"^(what\s+is|who\s+is|tell\s+me\s+about|what\s+do\s+we\s+know\s+about)\s+",
    re.IGNORECASE,
)


def _infer_budget(
    query: str,
    explicit_budget: int,
    classify_fn: Callable[[str], QueryIntent] | None = None,
) -> int:
    """Automatically adjust the token budget based on question type.

    Quick lookups (person/company names, keywords) get a small budget.
    Research-style questions get a larger one. If the caller explicitly
    set a budget, that value is used unchanged.

    Args:
        classify_fn: Injectable intent classifier for testing.
                     Defaults to ``kairix.core.search.intent.classify``.
    """
    if explicit_budget != 3000:
        return explicit_budget
    try:
        from kairix.core.search.intent import QueryIntent as _QueryIntent
        from kairix.core.search.intent import classify as _default_classify

        _classify = classify_fn or _default_classify
        intent = _classify(query)
        if intent in (_QueryIntent.ENTITY, _QueryIntent.KEYWORD):
            return 1500
    except (ImportError, ValueError, TypeError, RuntimeError):
        logger.debug("_infer_budget: classify failed, using heuristics", exc_info=True)
    if _RESEARCH_WORDS.search(query):
        return 5000
    return 3000


def _extract_entity_name(query: str) -> str:
    """Best-effort extraction of the entity name from a query string."""
    name = _ENTITY_PREFIX_RE.sub("", query).strip()
    return name.rstrip("?!. ")


# ---------------------------------------------------------------------------
# Shared service helpers — MCP tools call these, not each other
# ---------------------------------------------------------------------------


def _fetch_entity_card(name: str, *, neo4j_client: Any | None = None) -> dict[str, Any] | None:
    """Fetch entity card directly from Neo4j, bypassing MCP tool layer.

    Returns a dict with id, name, type, summary, vault_path on success,
    or None if the entity is not found or Neo4j is unavailable.

    Args:
        neo4j_client: Injectable Neo4j client for testing.
                      Defaults to the production client.
    """
    try:
        from kairix.utils import slugify as _slugify

        if neo4j_client is not None:
            neo4j = neo4j_client
        else:
            from kairix.knowledge.graph.client import get_client

            neo4j = get_client()
        if not neo4j.available:
            return None

        slug = _slugify(name)
        rows = neo4j.cypher(
            "MATCH (n) WHERE n.id = $id OR toLower(n.name) = toLower($name) "
            "RETURN labels(n)[0] AS type, n.id AS id, n.name AS name, "
            "n.vault_path AS vault_path, "
            "n.role AS role, n.org AS org, "
            "n.tier AS tier, n.engagement_status AS engagement_status, "
            "n.domain AS domain, n.industry AS industry, "
            "n.category AS category "
            "LIMIT 1",
            {"id": slug, "name": name},
        )
        if rows:
            r = rows[0]
            # Build summary from type-specific fields
            summary_parts: list[str] = []
            if r.get("role"):
                summary_parts.append(r["role"])
            if r.get("org"):
                summary_parts.append(f"at {r['org']}")
            if r.get("tier"):
                summary_parts.append(f"Tier {r['tier']}")
            if r.get("engagement_status"):
                summary_parts.append(f"({r['engagement_status']})")
            if r.get("industry"):
                val = r["industry"]
                summary_parts.append(", ".join(val) if isinstance(val, list) else val)
            if r.get("domain"):
                summary_parts.append(r["domain"])
            if r.get("category"):
                summary_parts.append(r["category"])
            return {
                "id": r.get("id", ""),
                "name": r.get("name", ""),
                "type": r.get("type", ""),
                "summary": " — ".join(summary_parts) if summary_parts else "",
                "vault_path": r.get("vault_path") or "",
            }
    except (ImportError, RuntimeError, OSError, KeyError) as exc:
        logger.warning("_fetch_entity_card failed: %s", exc, exc_info=True)

    return None


# ---------------------------------------------------------------------------
# Tool implementations — pure Python, no mcp dependency
# ---------------------------------------------------------------------------


def tool_search(
    query: str,
    agent: str | None = None,
    scope: Scope = Scope.SHARED_AGENT,
    budget: int = 3000,
    *,
    search_fn: Callable[..., Any] | None = None,
    entity_card_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Search for anything in the knowledge base.

    Just ask your question — the system works out the best way to find
    what you need, including handling date-based queries (temporal) and
    setting the right amount of context automatically. You don't need
    to configure anything.

    Args:
        search_fn:      Injectable search function for testing.
                        Defaults to the production hybrid search.
        entity_card_fn: Injectable entity card function for testing.
                        Defaults to _fetch_entity_card.
    """
    logger.info("mcp.search: agent=%r scope=%r", agent, scope)
    try:
        # The ``search_fn is None`` branch only fires when ``kairix.core.factory``
        # is uninstalled; in-process tests always inject ``search_fn`` directly,
        # so the lazy-import path is unreachable under test.
        if search_fn is None:  # pragma: no cover
            from kairix.core.factory import build_search_pipeline

            _pipeline = build_search_pipeline()
            search_fn = _pipeline.search

        budget = _infer_budget(query, budget)
        result = search_fn(query=query, agent=agent, scope=scope, budget=budget)

        intent_value = result.intent.value if hasattr(result.intent, "value") else str(result.intent)
        results_list = [
            {
                "path": r.result.path,
                "score": r.result.boosted_score,
                "snippet": r.content[:500],
                "tokens": r.token_estimate,
            }
            for r in result.results
        ]

        # AFF-3: When the question is about a known person/company, show
        # their knowledge graph summary at the top of results.
        # Uses _fetch_entity_card (direct Neo4j call) — MCP tools should
        # not call other MCP tools; they share underlying services.
        if intent_value == QueryIntent.ENTITY.value:
            entity_name = _extract_entity_name(query)
            if entity_name:
                _entity_card = entity_card_fn or _fetch_entity_card
                card = _entity_card(entity_name)
                if card is not None:
                    results_list.insert(
                        0,
                        {
                            "path": card.get("vault_path", ""),
                            "score": 1.0,
                            "snippet": card.get("summary", ""),
                            "tokens": estimate_tokens(card.get("summary", "")),
                            "source": "entity_graph",
                            "entity": {
                                "id": card.get("id", ""),
                                "name": card.get("name", ""),
                                "type": card.get("type", ""),
                            },
                        },
                    )

        return {
            "query": result.query,
            "intent": intent_value,
            "results": results_list,
            "total_tokens": result.total_tokens,
            "latency_ms": result.latency_ms,
            "error": result.error,
        }
    except (
        ImportError,
        sqlite3.Error,
        requests.RequestException,
        KeyError,
        ValueError,
    ) as exc:
        logger.warning("mcp.search failed: %s", exc, exc_info=True)
        return {
            "query": query,
            "intent": "",
            "results": [],
            "total_tokens": 0,
            "latency_ms": 0.0,
            "error": "Search failed — check server logs for details.",
        }
    except Exception as exc:  # broad catch justified: tool_search must never raise to MCP callers
        logger.warning("mcp.search failed (unexpected): %s", exc, exc_info=True)
        return {
            "query": query,
            "intent": "",
            "results": [],
            "total_tokens": 0,
            "latency_ms": 0.0,
            "error": "Search failed — check server logs for details.",
        }


def tool_entity(
    name: str,
    *,
    neo4j_client: Any | None = None,
) -> dict[str, Any]:
    """Look up a specific person, company, or topic by name.

    This is a quick, direct lookup from the knowledge graph (Neo4j) —
    use it when you already know the name of what you're looking for.

    Args:
        neo4j_client: Injectable Neo4j client for testing.
                      Defaults to the production client.
    """
    card = _fetch_entity_card(name, neo4j_client=neo4j_client)
    if card is not None:
        return {**card, "error": ""}

    return {
        "id": "",
        "name": name,
        "type": "",
        "summary": "",
        "vault_path": "",
        "error": f"Entity not found: {name}",
    }


def tool_prep(
    query: str,
    agent: str | None = None,
    tier: Literal["l0", "l1"] = "l0",
    scope: Scope = DEFAULT_SCOPE,
    *,
    search_fn: Callable[..., Any] | None = None,
    chat_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Get a short summary of a topic before committing to a full search.

    Choose 'l0' for 2-3 sentences or 'l1' for a structured overview.
    Uses less resources than a full search — good for quick context checks.
    Retrieves relevant documents first, then summarises from them.

    Args:
        scope:     Search scope — shared, agent, shared+agent, all-agents,
                   or everything. Default shared+agent.
        search_fn: Injectable search function for testing.
                   Defaults to the production hybrid search.
        chat_fn:   Injectable chat completion function for testing.
                   Defaults to the production Azure chat completion.
    """
    try:
        if search_fn is None:
            from kairix.core.factory import build_search_pipeline

            _pipeline = build_search_pipeline()
            search_fn = _pipeline.search
        if chat_fn is None:
            from kairix._azure import chat_completion

            chat_fn = chat_completion

        # Retrieve context first — prep is grounded, not hallucinated
        budget = 1500 if tier == "l0" else 3000
        sr = search_fn(query, agent=agent, scope=scope, budget=budget)
        context_parts = []
        for r in sr.results[:5]:
            context_parts.append(f"[{r.result.title or r.result.path}]\n{r.content[:500]}")
        context = "\n\n---\n\n".join(context_parts) if context_parts else ""

        if not context:
            return {
                "query": query,
                "tier": tier,
                "summary": "No relevant documents found for this topic.",
                "tokens": 0,
                "error": "",
            }

        max_tokens = 150 if tier == "l0" else 600
        system = (
            "You are a concise knowledge assistant. Based ONLY on the provided documents, "
            "summarise what is known about the topic in 2-3 sentences. "
            "Do not add information that is not in the documents."
            if tier == "l0"
            else "You are a knowledge assistant. Based ONLY on the provided documents, "
            "provide a structured overview of the topic. "
            "Do not add information that is not in the documents."
        )
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Topic: {query}\n\nDocuments:\n{context}"},
        ]
        summary = chat_fn(messages, max_tokens=max_tokens)
        return {
            "query": query,
            "tier": tier,
            "summary": summary,
            "tokens": estimate_tokens(summary),
            "sources": [r.result.title or r.result.path for r in sr.results[:5]],
            "error": "",
        }
    except (ImportError, OSError, RuntimeError, KeyError, ValueError) as exc:
        logger.warning("mcp.prep failed: %s", exc, exc_info=True)
        return {
            "query": query,
            "tier": tier,
            "summary": "",
            "tokens": 0,
            "sources": [],
            "error": "Prep failed — check server logs for details.",
        }


def tool_timeline(
    query: str,
    anchor_date: str | None = None,
    agent: str | None = None,
    scope: Scope = DEFAULT_SCOPE,
    *,
    extract_fn: Callable[..., Any] | None = None,
    rewrite_fn: Callable[..., Any] | None = None,
    search_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Date-aware retrieval: rewrite a temporal query and fetch results.

    Behaviour matches the CLI's ``kairix timeline`` (closes the WS2-C asymmetry
    flagged in the 2026-05-02 dogfood):

      - When the query contains a temporal expression ("last week", "April 2026"),
        the rewriter expands it into a date range; ``rewritten_query`` carries
        the expanded form.
      - When no temporal expression is found, ``is_temporal=False`` and
        ``fell_back=True``; the search is performed against the original query
        rather than returning nothing useful.
      - Either way, ``results`` carries the search hits when ``search_fn`` is
        provided. Tests that don't want the search side-effect omit ``search_fn``
        and get an empty ``results`` list.

    Args:
        extract_fn: Injectable time window extraction for testing. Defaults to
            extract_time_window in production via build_server wiring.
        rewrite_fn: Injectable temporal rewriter for testing.
        search_fn:  Injectable search function. When None, no search is
            performed (rewriter-only mode for unit tests). Production
            invocations from build_server pass a wired SearchPipeline.search.
    """
    try:
        from datetime import date as _date

        if extract_fn is None:
            from kairix.core.temporal.rewriter import extract_time_window

            extract_fn = extract_time_window
        if rewrite_fn is None:
            from kairix.core.temporal.rewriter import rewrite_temporal_query

            rewrite_fn = rewrite_temporal_query

        anchor: _date | None = None
        if anchor_date:
            try:
                anchor = _date.fromisoformat(anchor_date)
            except ValueError:
                pass

        # Detect temporal intent from BOTH relative ("last week") and absolute ("April 2026") expressions
        time_window: dict[str, str] = {}
        try:
            start, end = extract_fn(query=query, reference_date=anchor)
            if start or end:
                time_window = {
                    "start": str(start) if start else "",
                    "end": str(end) if end else "",
                }
        except Exception:
            start, end = None, None
            logger.debug("extract_time_window failed", exc_info=True)

        is_temporal = bool(time_window)
        rewritten = rewrite_fn(query=query, reference_date=anchor) if is_temporal else query
        rewritten = rewritten if rewritten is not None else query

        # Fallthrough search: when search_fn is wired, always run a search using
        # the (possibly rewritten) query so MCP callers get results even on
        # non-temporal queries. fell_back signals the rewrite was a no-op.
        # Result items are BudgetedResult — fields live on .result; .content
        # is the boundary-trimmed snippet. Earlier code shape-mismatched and
        # produced empty placeholders (Shape's 2026-05-05 MCP path report).
        results_list: list[dict[str, Any]] = []
        if search_fn is not None:
            try:
                sr = search_fn(query=rewritten, budget=3000, agent=agent, scope=scope)
                for budgeted in getattr(sr, "results", []):
                    inner = getattr(budgeted, "result", None)
                    if inner is None:
                        continue
                    snippet = getattr(budgeted, "content", "") or getattr(inner, "snippet", "")
                    results_list.append(
                        {
                            "path": getattr(inner, "path", ""),
                            "title": getattr(inner, "title", ""),
                            "snippet": snippet[:300],
                            "score": getattr(inner, "boosted_score", getattr(inner, "score", 0.0)),
                        }
                    )
                # Diagnostic for #163: when results are non-empty but every row
                # has an empty path, the upstream pipeline is producing a
                # malformed result shape. Log enough state to triage the next
                # occurrence without reproducing live (the CLI is unaffected
                # because it uses a different code path entirely —
                # query_temporal_chunks against the structured temporal index).
                if results_list and all(not r["path"] for r in results_list):
                    logger.warning(
                        "mcp.timeline: %d empty-path results returned for query=%r "
                        "(rewritten=%r, agent=%r, scope=%r). Pipeline diagnostics: "
                        "bm25_count=%s vec_count=%s fused_count=%s collections=%s "
                        "vec_failed=%s error=%r — see #163.",
                        len(results_list),
                        query[:80],
                        rewritten[:80] if rewritten != query else "<same>",
                        agent,
                        scope.value if hasattr(scope, "value") else str(scope),
                        getattr(sr, "bm25_count", "?"),
                        getattr(sr, "vec_count", "?"),
                        getattr(sr, "fused_count", "?"),
                        getattr(sr, "collections", "?"),
                        getattr(sr, "vec_failed", "?"),
                        getattr(sr, "error", "?"),
                    )
            except Exception:
                logger.debug("timeline fallthrough search failed", exc_info=True)

        return {
            "original_query": query,
            "rewritten_query": rewritten,
            "is_temporal": is_temporal,
            "fell_back": not is_temporal,
            "time_window": time_window,
            "results": results_list,
            "error": "",
        }
    except Exception as exc:
        logger.warning("mcp.timeline failed: %s", exc, exc_info=True)
        return {
            "original_query": query,
            "rewritten_query": query,
            "is_temporal": False,
            "fell_back": True,
            "time_window": {},
            "results": [],
            "error": "Timeline processing failed — check server logs for details.",
        }


def tool_research(
    query: str,
    agent: str | None = None,
    max_turns: int = 4,
    *,
    research_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Ask a research question. The system searches multiple times, refining
    its approach until it finds a good answer or reports what's missing.

    Use this for complex questions that need more than a quick search.
    For simple lookups, use search instead — it's faster.

    Args:
        query:       The question to research.
        agent:       Agent name for collection scoping.
        max_turns:   Maximum search rounds (default 4).
        research_fn: Injectable research function for testing.
                     Defaults to run_research.

    Returns:
        dict with: query, synthesis, retrieved_chunks, gaps, confidence, turns, error.
    """
    # Clamp max_turns to prevent unbounded LLM call amplification
    max_turns = min(max(1, max_turns), 10)
    try:
        if research_fn is None:
            from kairix.agents.research.graph import run_research

            research_fn = run_research

        result = research_fn(query=query, max_turns=max_turns)
        return {
            "query": result.get("query", query),
            "synthesis": result.get("synthesis", ""),
            "retrieved_chunks": result.get("retrieved_chunks", [])[:10],
            "gaps": result.get("gaps", []),
            "confidence": result.get("confidence", 0.0),
            "turns": result.get("turns", 0),
            "error": result.get("error", ""),
        }
    except Exception as exc:
        logger.warning("mcp.research failed: %s", exc, exc_info=True)
        return {
            "query": query,
            "synthesis": "",
            "retrieved_chunks": [],
            "gaps": [],
            "confidence": 0.0,
            "turns": 0,
            "error": "Research failed — check server logs for details.",
        }


def tool_usage_guide(topic: str = "", *, guide_path: Path | None = None) -> dict[str, Any]:
    """
    Return the kairix agent usage guide, or a section of it filtered by topic.

    Use this tool when you are unsure how to use kairix, when a search returns
    unexpected results, or when you want to understand a specific feature.

    Args:
        topic: Optional topic filter (e.g. "temporal", "entity", "troubleshoot",
               "intent", "budget"). Empty string returns the full guide.
        guide_path: Explicit path to the guide markdown file. When omitted,
                    resolves relative to the package layout (production default).
                    Tests inject an explicit path rather than monkeypatching
                    ``__file__``.

    Returns:
        dict with keys: topic, content (markdown string), error.
    """
    try:
        if guide_path is None:
            # Production-default lookup: try relative to this file, then to the
            # kairix package root.
            guide_path = Path(__file__).parent.parent.parent / "docs" / "agent-usage-guide.md"
            if not guide_path.exists():
                import kairix as _kairix

                guide_path = Path(_kairix.__file__).parent.parent / "docs" / "agent-usage-guide.md"

        if not guide_path.exists():
            return {
                "topic": topic,
                "content": "",
                "error": "Usage guide not found. Run: kairix onboard guide --document-root <path>",
            }

        full_text = guide_path.read_text(encoding="utf-8")

        if not topic:
            return {"topic": "", "content": full_text, "error": ""}

        # Filter to sections matching the topic (heading-level search)
        topic_lower = topic.lower()
        lines = full_text.splitlines()
        sections: list[str] = []
        in_section = False
        current: list[str] = []

        for line in lines:
            is_heading = line.startswith("## ") or line.startswith("### ")
            if is_heading:
                if in_section and current:
                    sections.append("\n".join(current))
                    current = []
                if topic_lower in line.lower():
                    in_section = True
                    current = [line]
                else:
                    in_section = False
            elif in_section:
                current.append(line)

        if in_section and current:
            sections.append("\n".join(current))

        if not sections:
            # Fallback: search for topic keyword in full text
            matching_lines = [ln for ln in lines if topic_lower in ln.lower()]
            content = "\n".join(matching_lines[:30]) if matching_lines else full_text[:2000]
        else:
            content = "\n\n".join(sections)

        return {"topic": topic, "content": content, "error": ""}

    except Exception as exc:
        logger.warning("mcp.usage_guide failed: %s", exc)
        return {
            "topic": topic,
            "content": "",
            "error": "Usage guide lookup failed — check server logs for details.",
        }


def tool_contradict(
    content: str,
    agent: str | None = None,
    top_k: int = 5,
    threshold: float = 0.45,
    scope: Scope = DEFAULT_SCOPE,
    *,
    llm_backend: Any | None = None,
    contradict_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Check new content against existing knowledge for contradictions.

    Use before writing new facts — catches conflicts with what's already
    in the knowledge base. Returns a list of contradicting documents with
    scores and explanations.

    Args:
        llm_backend:   Injectable LLM backend for testing.
                       Defaults to the production backend.
        contradict_fn: Injectable contradiction checker for testing.
                       Defaults to check_contradiction.
    """
    try:
        if contradict_fn is None:
            from kairix.knowledge.contradict.detector import check_contradiction

            contradict_fn = check_contradiction
        if llm_backend is None:
            from kairix.platform.llm import get_default_backend

            llm_backend = get_default_backend()

        # Agent forwarding stays conditional — the WS2-B design intentionally
        # doesn't lock contradict to a single agent's collection so cross-agent
        # contradictions surface. Scope is always forwarded so callers who want
        # explicit broadening (scope=everything) or narrowing get it.
        extra: dict[str, Any] = {"scope": scope}
        if agent is not None:
            extra["agent"] = agent
        results = contradict_fn(
            content=content,
            llm=llm_backend,
            top_k=top_k,
            threshold=threshold,
            **extra,
        )
        return {
            "content": content,
            "contradictions": [
                {
                    "path": r.doc_path,
                    "score": r.score,
                    "reason": r.reason,
                    "snippet": r.snippet,
                }
                for r in results
            ],
            "has_contradictions": len(results) > 0,
            "error": "",
        }
    except Exception as exc:
        logger.warning("mcp.contradict failed: %s", exc, exc_info=True)
        return {
            "content": content,
            "contradictions": [],
            "has_contradictions": False,
            "error": "Contradiction check failed — check server logs for details.",
        }


# ---------------------------------------------------------------------------
# FastMCP server — only constructed when mcp package is available
# ---------------------------------------------------------------------------


def build_server(host: str = "127.0.0.1", port: int = 8080) -> Any:
    """
    Construct and return the FastMCP server with all tools registered.

    Args:
        host: Bind address for SSE transport.
        port: Port for SSE transport.

    Raises ImportError when the ``mcp`` package is not installed.
    Install via: pip install kairix[agents]
    """
    try:
        from mcp.server.fastmcp import FastMCP
    # The ImportError branch is reachable only when the optional ``mcp`` extra
    # is not installed; the test suite always installs it via ``kairix[agents]``.
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "The 'mcp' package is required to run the MCP server. Install it with: pip install 'kairix[agents]'"
        ) from exc

    server = FastMCP("kairix", host=host, port=port)

    @server.tool()
    @wrap_tool_errors
    def search(
        query: str,
        agent: str | None = None,
        scope: Scope = DEFAULT_SCOPE,
        budget: int = 3000,
    ) -> dict[str, Any]:
        """Search your knowledge store — finds the best answers to any question."""
        return tool_search(query=query, agent=agent, scope=scope, budget=budget)

    @server.tool()
    @wrap_tool_errors
    def entity(name: str) -> dict[str, Any]:
        """Entity lookup from Neo4j."""
        return tool_entity(name=name)

    @server.tool()
    @wrap_tool_errors
    def prep(
        query: str,
        agent: str | None = None,
        tier: Literal["l0", "l1"] = "l0",
        scope: Scope = DEFAULT_SCOPE,
    ) -> dict[str, Any]:
        """Context preparation: tiered L0/L1 summary generation."""
        return tool_prep(query=query, agent=agent, tier=tier, scope=scope)

    @server.tool()
    @wrap_tool_errors
    def timeline(
        query: str,
        anchor_date: str | None = None,
        agent: str | None = None,
        scope: Scope = DEFAULT_SCOPE,
    ) -> dict[str, Any]:
        """Temporal query rewriting + date-aware retrieval."""
        from kairix.core.factory import build_search_pipeline

        _timeline_pipeline = build_search_pipeline()
        return tool_timeline(
            query=query,
            anchor_date=anchor_date,
            agent=agent,
            scope=scope,
            search_fn=_timeline_pipeline.search,
        )

    @server.tool()
    @wrap_tool_errors
    def research(query: str, agent: str | None = None, max_turns: int = 4) -> dict[str, Any]:
        """Research a complex question. Searches iteratively until it finds a good answer."""
        return tool_research(query=query, agent=agent, max_turns=max_turns)

    @server.tool()
    @wrap_tool_errors
    def contradict(
        content: str,
        agent: str | None = None,
        top_k: int = 5,
        threshold: float = 0.45,
        scope: Scope = DEFAULT_SCOPE,
    ) -> dict[str, Any]:
        """Check new content against existing knowledge for contradictions."""
        return tool_contradict(
            content=content,
            agent=agent,
            top_k=top_k,
            threshold=threshold,
            scope=scope,
        )

    @server.tool()
    @wrap_tool_errors
    def usage_guide(topic: str = "") -> dict[str, Any]:
        """Return the kairix agent usage guide. Call this when unsure how to use kairix."""
        return tool_usage_guide(topic=topic)

    return server
