"""
kairix.agents.mcp.server — MCP server exposing kairix tools to MCP-compatible agents.

Provides the following tools:
  bootstrap    Agent orientation envelope: role, board, recent memory, goals, health
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
from pathlib import Path
from typing import Any, Literal

from kairix.agents.mcp.errors import async_tool_handler
from kairix.core.search.scope import Scope

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_SCOPE: Scope = Scope.SHARED_AGENT

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
    limit: int = 10,
    *,
    deps: Any = None,
) -> dict[str, Any]:
    """Search the knowledge store.

    Thin adapter around ``kairix.use_cases.search.run_search``. CLI and
    MCP both delegate to the same use case so the surfaces stay aligned
    (closes Phase-2 drift in #168).

    The optional ``deps`` parameter forwards a ``SearchDeps`` directly
    to the use case — production callers leave it None; tests pass a
    ``SearchDeps`` to drive without touching live services.
    """
    from kairix.use_cases.search import run_search, search_output_to_envelope

    logger.info("mcp.search: agent=%r scope=%r", agent, scope)
    out = run_search(
        query,
        agent=agent,
        scope=scope,
        budget=budget,
        limit=limit,
        deps=deps,
    )
    return search_output_to_envelope(out)


def tool_entity(
    name: str,
    *,
    deps: Any = None,
    neo4j_client: Any | None = None,
) -> dict[str, Any]:
    """Look up a specific person, company, or topic by name.

    Thin adapter around ``kairix.use_cases.entity_get.run_entity_get``.
    This is a quick, direct lookup from the knowledge graph (Neo4j) —
    use it when you already know the name of what you're looking for.

    The optional ``deps`` parameter forwards an ``EntityGetDeps`` directly
    to the use case — production callers leave it None.

    The legacy ``neo4j_client`` parameter is retained for back-compat;
    when set, it overrides the default ``_fetch_entity_card`` helper's
    Neo4j client. Prefer ``deps`` for new code.
    """
    from kairix.use_cases.entity_get import EntityGetDeps, entity_get_output_to_envelope, run_entity_get

    if deps is None and neo4j_client is not None:
        deps = EntityGetDeps(fetch_fn=lambda n: _fetch_entity_card(n, neo4j_client=neo4j_client))

    out = run_entity_get(name, deps=deps)
    return entity_get_output_to_envelope(out)


def tool_prep(
    query: str,
    agent: str | None = None,
    tier: Literal["l0", "l1"] = "l0",
    scope: Scope = DEFAULT_SCOPE,
    *,
    deps: Any = None,
) -> dict[str, Any]:
    """Get a short summary of a topic before committing to a full search.

    Thin adapter around ``kairix.use_cases.prep.run_prep``. Choose 'l0'
    for 2-3 sentences or 'l1' for a structured overview. Uses less
    resources than a full search — good for quick context checks.
    Retrieves relevant documents first, then summarises from them.

    The optional ``deps`` parameter forwards a ``PrepDeps`` directly
    to the use case — production callers leave it None.
    """
    from kairix.use_cases.prep import prep_output_to_envelope, run_prep

    out = run_prep(query, agent=agent, scope=scope, tier=tier, deps=deps)
    return prep_output_to_envelope(out)


def tool_timeline(
    query: str,
    anchor_date: str | None = None,
    agent: str | None = None,
    scope: Scope = DEFAULT_SCOPE,
) -> dict[str, Any]:
    """Date-aware retrieval: rewrite a temporal query and fetch results.

    Thin adapter around ``kairix.use_cases.timeline.run_timeline``. CLI and
    MCP both call the same use case so behaviour is identical (closes #163,
    Phase 1 of #168). The use case extracts a time window from the query
    (or accepts explicit since/until), tries the temporal-chunks index
    first, then falls through to the search pipeline.
    """
    from datetime import date as _date

    from kairix.use_cases.timeline import run_timeline

    anchor: _date | None = None
    if anchor_date:
        try:
            anchor = _date.fromisoformat(anchor_date)
        except ValueError:
            pass

    result = run_timeline(
        query,
        anchor_date=anchor,
        agent=agent,
        scope=scope,
    )

    return {
        "original_query": result.original_query,
        "rewritten_query": result.rewritten_query,
        "is_temporal": result.is_temporal,
        "fell_back": result.fell_back,
        "time_window": result.time_window,
        "results": [
            {
                "path": h.path,
                "title": h.title,
                "snippet": h.snippet,
                "score": h.score,
            }
            for h in result.results
        ],
        "error": result.error,
    }


def tool_research(
    query: str,
    agent: str | None = None,
    max_turns: int = 4,
    *,
    deps: Any = None,
) -> dict[str, Any]:
    """Ask a research question. The system searches multiple times, refining
    its approach until it finds a good answer or reports what's missing.

    Thin adapter around ``kairix.use_cases.research.run_research_use_case``.
    Use this for complex questions that need more than a quick search.
    For simple lookups, use search instead — it's faster.

    The optional ``deps`` parameter forwards a ``ResearchDeps`` directly
    to the use case — production callers leave it None.
    """
    from kairix.use_cases.research import research_output_to_envelope, run_research_use_case

    out = run_research_use_case(query, max_turns=max_turns, deps=deps)
    return research_output_to_envelope(out)


def tool_usage_guide(
    topic: str = "",
    *,
    guide_path: Path | None = None,
    deps: Any = None,
) -> dict[str, Any]:
    """
    Return the kairix agent usage guide, or a section of it filtered by topic.

    Thin adapter around ``kairix.use_cases.usage_guide.run_usage_guide``.
    Use this tool when you are unsure how to use kairix, when a search
    returns unexpected results, or when you want to understand a feature.

    The optional ``deps`` parameter forwards a ``UsageGuideDeps`` directly
    to the use case — production callers leave it None. The legacy
    ``guide_path`` parameter is preserved as the operator-facing override.
    """
    from kairix.use_cases.usage_guide import run_usage_guide, usage_guide_output_to_envelope

    out = run_usage_guide(topic, guide_path=guide_path, deps=deps)
    return usage_guide_output_to_envelope(out)


def tool_contradict(
    content: str,
    agent: str | None = None,
    top_k: int = 5,
    threshold: float = 0.45,
    top_claims: int = 3,
    scope: Scope = DEFAULT_SCOPE,
    *,
    deps: Any = None,
) -> dict[str, Any]:
    """Check new content against existing knowledge for contradictions.

    Thin adapter around ``kairix.use_cases.contradict.run_contradict``.
    Use before writing new facts — catches conflicts with what's already
    in the knowledge base. Returns a list of contradicting documents with
    scores and explanations.

    The optional ``deps`` parameter forwards a ``ContradictDeps`` directly
    to the use case — production callers leave it None.
    """
    from kairix.use_cases.contradict import contradict_output_to_envelope, run_contradict

    out = run_contradict(
        content,
        agent=agent,
        scope=scope,
        top_k=top_k,
        threshold=threshold,
        top_claims=top_claims,
        deps=deps,
    )
    return contradict_output_to_envelope(out)


def tool_entity_suggest(
    text: str,
    *,
    deps: Any = None,
) -> dict[str, Any]:
    """Suggest entities found in arbitrary text by running NER + Neo4j cross-ref.

    Thin adapter around ``kairix.use_cases.entity.run_entity_suggest``.
    Use to spot people, organisations, places mentioned in prose so an
    operator (or another agent) can decide whether to add them to the
    knowledge graph.
    """
    from kairix.use_cases.entity import entity_suggest_output_to_envelope, run_entity_suggest

    out = run_entity_suggest(text, deps=deps)
    return entity_suggest_output_to_envelope(out)


def tool_entity_validate(
    name: str,
    update: bool = False,
    *,
    deps: Any = None,
) -> dict[str, Any]:
    """Validate an entity against Wikidata and optionally update Neo4j.

    Thin adapter around ``kairix.use_cases.entity.run_entity_validate``.
    Use to confirm a graph entity has a real-world match (qid) and
    optionally write that qid back to the Neo4j node.
    """
    from kairix.use_cases.entity import entity_validate_output_to_envelope, run_entity_validate

    out = run_entity_validate(name, update=update, deps=deps)
    return entity_validate_output_to_envelope(out)


def tool_brief(
    agent: str,
    *,
    deps: Any = None,
) -> dict[str, Any]:
    """Generate a session briefing and return its content + path.

    Thin adapter around ``kairix.use_cases.brief.run_brief``. Use before
    starting work — gives an agent the operator's recent decisions,
    notes, and entity stub in one structured payload.

    The optional ``deps`` parameter forwards a ``BriefDeps`` directly to
    the use case — production callers leave it None.
    """
    from kairix.use_cases.brief import brief_output_to_envelope, run_brief

    out = run_brief(agent, deps=deps)
    return brief_output_to_envelope(out)


def tool_bootstrap(
    agent: str,
    max_memory_days: int = 3,
    *,
    deps: Any = None,
) -> dict[str, Any]:
    """Return the agent orientation envelope (#246 W1).

    Thin adapter around ``kairix.use_cases.bootstrap.run_bootstrap``.
    Returns the agent's role, current ``Board.md``, recent memory
    entries, active goals, and a structured health snapshot — the
    single call an agent makes at session start (or topic switch) to
    absorb its current state. Never raises; degradation is surfaced via
    the ``health`` field with a prescriptive ``next_action``.

    The optional ``deps`` parameter forwards a ``BootstrapDeps`` directly
    to the use case — production callers leave it None.
    """
    from kairix.use_cases.bootstrap import bootstrap_output_to_envelope, run_bootstrap

    out = run_bootstrap(agent, deps=deps, max_memory_days=max_memory_days)
    return bootstrap_output_to_envelope(out)


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
    except ImportError as exc:  # pragma: no cover — optional 'mcp' extra; tests always install kairix[agents]
        raise ImportError(
            "The 'mcp' package is required to run the MCP server. Install it with: pip install 'kairix[agents]'"
        ) from exc

    server = FastMCP("kairix", host=host, port=port)

    @server.tool(
        description=(
            "Call before answering any factual question about prior work, decisions, or context — "
            "kairix indexes the team's knowledge store and finds relevant prior material. "
            "Use this proactively at session start and whenever a question touches the team's history."
        )
    )
    @async_tool_handler
    def search(
        query: str,
        agent: str | None = None,
        scope: Scope = DEFAULT_SCOPE,
        budget: int = 3000,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Search your knowledge store — finds the best answers to any question."""
        return tool_search(query=query, agent=agent, scope=scope, budget=budget, limit=limit)

    @server.tool(
        description=(
            "Call when you need facts about a specific named entity (person, company, project) — "
            "direct knowledge-graph lookup, faster than search."
        )
    )
    @async_tool_handler
    def entity(name: str) -> dict[str, Any]:
        """Entity lookup from Neo4j."""
        return tool_entity(name=name)

    @server.tool()
    @async_tool_handler
    def prep(
        query: str,
        agent: str | None = None,
        tier: Literal["l0", "l1"] = "l0",
        scope: Scope = DEFAULT_SCOPE,
    ) -> dict[str, Any]:
        """Context preparation: tiered L0/L1 summary generation."""
        return tool_prep(query=query, agent=agent, tier=tier, scope=scope)

    @server.tool()
    @async_tool_handler
    def timeline(
        query: str,
        anchor_date: str | None = None,
        agent: str | None = None,
        scope: Scope = DEFAULT_SCOPE,
    ) -> dict[str, Any]:
        """Temporal query rewriting + date-aware retrieval."""
        return tool_timeline(
            query=query,
            anchor_date=anchor_date,
            agent=agent,
            scope=scope,
        )

    @server.tool()
    @async_tool_handler
    def research(query: str, agent: str | None = None, max_turns: int = 4) -> dict[str, Any]:
        """Research a complex question. Searches iteratively until it finds a good answer."""
        return tool_research(query=query, agent=agent, max_turns=max_turns)

    @server.tool()
    @async_tool_handler
    def contradict(
        content: str,
        agent: str | None = None,
        top_k: int = 5,
        threshold: float = 0.45,
        top_claims: int = 3,
        scope: Scope = DEFAULT_SCOPE,
    ) -> dict[str, Any]:
        """Check new content against existing knowledge for contradictions."""
        return tool_contradict(
            content=content,
            agent=agent,
            top_k=top_k,
            threshold=threshold,
            top_claims=top_claims,
            scope=scope,
        )

    @server.tool()
    @async_tool_handler
    def usage_guide(topic: str = "") -> dict[str, Any]:
        """Return the kairix agent usage guide. Call this when unsure how to use kairix."""
        return tool_usage_guide(topic=topic)

    @server.tool(
        description=(
            "Call when you want a synthesised view of a topic — kairix runs a small research loop "
            "across the knowledge store and returns a structured briefing. "
            "Use it when you'd otherwise be tempted to summarise from memory."
        )
    )
    @async_tool_handler
    def brief(agent: str) -> dict[str, Any]:
        """Generate a session briefing for an agent. Returns content + on-disk path."""
        return tool_brief(agent=agent)

    @server.tool(
        description=(
            "Call at session start or whenever you switch topics. "
            "Returns your agent role, current board, recent memory, and active goals — "
            "orients you in the team's current state. "
            "If health.vector_search != 'ok', surface that to your human."
        )
    )
    @async_tool_handler
    def bootstrap(agent: str, max_memory_days: int = 3) -> dict[str, Any]:
        """Return the agent orientation envelope: role, board, recent memory, goals, health."""
        return tool_bootstrap(agent=agent, max_memory_days=max_memory_days)

    @server.tool()
    @async_tool_handler
    def entity_suggest(text: str) -> dict[str, Any]:
        """Suggest entities (people, organisations, places) found in text via NER + Neo4j cross-ref."""
        return tool_entity_suggest(text=text)

    @server.tool()
    @async_tool_handler
    def entity_validate(name: str, update: bool = False) -> dict[str, Any]:
        """Validate a named entity against Wikidata and optionally write the qid to Neo4j."""
        return tool_entity_validate(name=name, update=update)

    return server
