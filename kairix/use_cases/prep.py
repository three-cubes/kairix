"""Prep use case — tiered L0/L1 context summary shared by CLI and MCP.

Phase 3c of the CLI/MCP feature parity initiative (#168). Pre-Phase-3c
``prep`` was MCP-only — operators couldn't reproduce an agent's prep
output from a shell. This module wraps the existing tool_prep logic
so both surfaces call the same ``run_prep``.

Synthesis shape (#397 W-C C2 investigation): ``run_prep`` is a
single-section synthesis. One ``search(...)`` call + one
``_format_context(...)`` + one ``chat(messages=..., max_tokens=...)``
call. There is no per-section fan-out, so no ``asyncio.gather``
opportunity exists at this layer. The L0/L1 tier selector chooses
prompt + budget shape, not multiple sections to synthesise.
Re-evaluate when a future tier requirement adds parallel sub-syntheses.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from kairix.core.protocols import SourceRef
from kairix.core.search.prep_summary_cache import (
    DEFAULT_MAX_AGE_S as _PREP_DEFAULT_MAX_AGE_S,
)
from kairix.core.search.prep_summary_cache import (
    DEFAULT_MAX_ENTRIES as _PREP_DEFAULT_MAX_ENTRIES,
)
from kairix.core.search.prep_summary_cache import (
    PrepSummaryCache,
    make_prep_cache_key,
)
from kairix.core.search.scope import Scope
from kairix.paths import trace_enabled
from kairix.text import estimate_tokens
from kairix.use_cases.enumeration import complete_enumeration, default_expand_callable
from kairix.use_cases.expand import ExpandOutput

logger = logging.getLogger(__name__)


# Process-shared PrepSummaryCache. Lazy-initialised on first prep call
# so the env-var bounds (if any) are read once at startup. Mirrors the
# ``_QUERY_CACHES`` pattern in ``kairix.core.factory`` so the operator
# surface (``probe caches``) sees both caches via the same accessor
# pattern.
_PREP_SUMMARY_CACHE: PrepSummaryCache | None = None
_PREP_SUMMARY_CACHE_LOCK = threading.Lock()


def _resolve_prep_cache_path() -> Any:
    """Resolve the persistent prep-cache path, or ``None`` under pytest.

    Mirrors the embed_cache / query_cache singleton-path-resolver
    pattern. F4-clean: env reads stay at the paths boundary.
    """
    import os

    if os.environ.get("PYTEST_CURRENT_TEST"):
        return None
    try:  # pragma: no cover  # F4 test-bypass: production path resolution only fires outside pytest
        from kairix.paths import prep_cache_path

        return prep_cache_path()
    except Exception as exc:  # pragma: no cover  # same — production-only branch
        logger.warning(
            "PrepSummaryCache: failed to resolve persistence path — degrading to in-memory-only. cause: %s",
            exc,
        )
        return None


def _resolve_current_cfg_hash() -> str:
    """Return the cfg_hash of the most recent pipeline build (#411 Phase 2).

    Reads the on-disk marker written by :func:`kairix.core.factory._record_pipeline_build_marker`.
    When no marker exists or the file is unreadable, returns the empty
    string — the prep cache then writes rows under the empty cfg
    scope, preserving today's behaviour for clean installs.
    """
    try:  # pragma: no cover  # F4 test-bypass: marker read only fires outside pytest (under pytest path is None)
        from kairix.core.pipeline_cache_marker import PipelineCacheMarker
        from kairix.paths import pipeline_cache_path

        marker = PipelineCacheMarker(path=pipeline_cache_path())
        try:
            last = marker.last()
        finally:
            marker.close()
    except Exception as exc:  # pragma: no cover  # same — production-only branch
        logger.debug("PrepSummaryCache: failed to read marker — using empty cfg_hash. cause: %s", exc)
        return ""
    if last is None:  # pragma: no cover  # same
        return ""
    return last[0]  # pragma: no cover  # same


def _get_or_create_prep_summary_cache() -> PrepSummaryCache:
    """Return the process-shared :class:`PrepSummaryCache`, building it lazily.

    Mirrors :func:`kairix.core.factory._get_or_create_query_cache`. The
    cache's bounds are the module defaults today — env-var overrides
    can be threaded through the same pattern as ``KAIRIX_QUERY_CACHE_*``
    when an operator's prep workload demands it.

    #411 Phase 2 — wires the persistent SQLite layer when a real path
    is resolvable (not under pytest). The cfg_hash comes from the
    pipeline-build marker so prep cache rows invalidate when the
    pipeline cfg changes.
    """
    global _PREP_SUMMARY_CACHE
    with _PREP_SUMMARY_CACHE_LOCK:
        if _PREP_SUMMARY_CACHE is None:
            path = _resolve_prep_cache_path()
            cfg_hash = _resolve_current_cfg_hash() if path is not None else ""
            _PREP_SUMMARY_CACHE = PrepSummaryCache(
                max_entries=_PREP_DEFAULT_MAX_ENTRIES,
                max_age_s=_PREP_DEFAULT_MAX_AGE_S,
                path=path,
                cfg_hash=cfg_hash,
            )
        return _PREP_SUMMARY_CACHE


def get_prep_summary_cache() -> PrepSummaryCache:
    """Public accessor for the process-shared prep summary cache.

    Used by the ``kairix caches`` CLI to surface hit / miss /
    eviction counts. Going through this helper keeps the module-global
    hidden so callers can't accidentally rebind ``_PREP_SUMMARY_CACHE``.
    """
    return _get_or_create_prep_summary_cache()


def reset_prep_summary_cache() -> None:
    """Drop every cached prep summary. Tests + operator reload paths call this."""
    with _PREP_SUMMARY_CACHE_LOCK:
        if _PREP_SUMMARY_CACHE is not None:
            _PREP_SUMMARY_CACHE.clear()


_L0_BUDGET = 1500
_L1_BUDGET = 3000
_L0_MAX_TOKENS = 150
_L1_MAX_TOKENS = 600


def _build_production_search_pipeline() -> Any:
    from kairix.core.factory import build_search_pipeline

    return build_search_pipeline()


def _resolve_production_provider_name() -> str | None:
    from kairix.paths import provider_name

    return provider_name()


def _resolve_production_provider(name: str) -> Any:
    from kairix.providers import get_provider

    return get_provider(name)


def default_search_callable(
    *,
    pipeline_factory: Callable[[], Any] = _build_production_search_pipeline,
    **kwargs: Any,
) -> Any:
    """Production search adapter used by ``PrepDeps`` when no override is passed.

    The ``pipeline_factory`` kwarg is the public DI seam: tests pass a fake
    factory returning a stub pipeline whose ``.search(**kwargs)`` returns the
    desired ``SearchResult`` shape, exercising this adapter end-to-end.
    """
    pipeline = pipeline_factory()
    return pipeline.search(**kwargs)


def _default_chat_backend(provider: Any) -> Any:
    """Production chat backend — wrap ``provider`` in :class:`ProviderChatBackend`."""
    from kairix.transport.embed_service import ProviderChatBackend

    return ProviderChatBackend(provider)


@dataclass
class ChatAdapterDeps:
    """Injectable collaborators for :func:`default_chat_callable`.

    Canonical Deps shape (``kairix/worker.py::WorkerDeps``). Production
    omits ``deps`` and resolves the configured plugin; tests construct
    ``ChatAdapterDeps(provider_name=..., resolve_provider=..., make_backend=...)``.

    - ``provider_name``: returns the configured ``provider:`` name (or
      ``None`` when unset). Default reads ``kairix.paths.provider_name``.
    - ``resolve_provider``: ``(name) -> Provider`` plugin lookup. Default
      ``kairix.providers.get_provider``.
    - ``make_backend``: ``(provider) -> backend`` with a ``.chat(**kwargs)``
      method. Default wraps in :class:`ProviderChatBackend`.
    """

    provider_name: Callable[[], str | None] = field(default_factory=lambda: _resolve_production_provider_name)
    resolve_provider: Callable[[str], Any] = field(default_factory=lambda: _resolve_production_provider)
    make_backend: Callable[[Any], Any] = field(default_factory=lambda: _default_chat_backend)


def default_chat_callable(
    *,
    deps: ChatAdapterDeps | None = None,
    **kwargs: Any,
) -> str:
    """Production chat adapter used by ``PrepDeps`` when no override is passed.

    Resolves the configured plugin via ``deps.provider_name`` +
    ``deps.resolve_provider``, wraps it via ``deps.make_backend`` (default
    :class:`ProviderChatBackend`), and forwards ``**kwargs`` to
    ``backend.chat``. Raises ``ValueError`` when no provider is configured —
    surfacing a config error at the boundary rather than letting the call
    vanish into a generic plugin failure.
    """
    deps = deps if deps is not None else ChatAdapterDeps()
    name = deps.provider_name()
    if name is None:
        raise ValueError("kairix.config.yaml is missing the required 'provider:' field")
    provider = deps.resolve_provider(name)
    backend = deps.make_backend(provider)
    reply: str = backend.chat(**kwargs)
    return reply


@dataclass(frozen=True)
class PrepOutput:
    """Outcome of one ``run_prep`` invocation.

    Attributes:
        query: The caller's query, unchanged.
        tier: Either ``"l0"`` (2-3 sentences) or ``"l1"`` (structured overview).
        summary: LLM-generated summary grounded in retrieved documents.
            Empty when no relevant documents were found, or on error.
        tokens: Estimated token count of ``summary``.
        sources: Up to 5 ``SourceRef`` breadcrumbs for the documents used
            as context. PLA-274 / #437 — pre-fix this was a list of human
            TITLE strings (not resolvable), so an agent reading a prep
            summary couldn't re-open the grounding sources. It is now a
            list of resolvable ``SourceRef``s (source_uri / path / page).
        error: Empty on success; structured ``"<Class>: <msg>"`` on
            top-level failure.
    """

    query: str
    tier: str
    summary: str = ""
    tokens: int = 0
    sources: list[SourceRef] = field(default_factory=list)
    error: str = ""

    @classmethod
    def from_envelope(cls, envelope: dict[str, Any]) -> PrepOutput:
        """Rebuild a ``PrepOutput`` from the dict ``prep_output_to_envelope`` emits.

        The seam for warm-MCP text-mode routing (#421 PR 2.4). The CLI
        dispatcher receives a JSON envelope from the MCP worker; this
        adapter projects it back to the dataclass shape ``format_text``
        already consumes, so the in-process and warm paths render
        byte-identical text.

        ``sources`` is rebuilt into ``SourceRef``s (PLA-274). Each source
        dict round-trips through ``SourceRef.from_envelope``; a legacy
        envelope that carried bare title strings degrades gracefully —
        ``from_envelope`` of a non-mapping yields an empty ref, so the
        renderer never crashes on an older worker's output.
        """
        sources_raw = envelope.get("sources", []) or []
        sources: list[SourceRef] = [SourceRef.from_envelope(s) for s in sources_raw if isinstance(s, Mapping)]
        return cls(
            query=str(envelope.get("query", "")),
            tier=str(envelope.get("tier", "")),
            summary=str(envelope.get("summary", "")),
            tokens=int(envelope.get("tokens", 0)),
            sources=sources,
            error=str(envelope.get("error", "")),
        )


@dataclass(frozen=True)
class PrepDeps:
    """Injectable dependencies for ``run_prep``.

    Non-Optional fields wired to production defaults via ``default_factory``
    — eliminates the ``Optional[Callable]`` mypy regression class flagged
    in #204. Tests construct ``PrepDeps(search_fn=fake, chat_fn=fake)``
    with explicit overrides; ``PrepDeps()`` with no kwargs resolves to
    the production callables defined above.
    """

    search_fn: Callable[..., Any] = field(default_factory=lambda: default_search_callable)
    chat_fn: Callable[..., str] = field(default_factory=lambda: default_chat_callable)
    # #437 — source-cohesion enumeration completion. When the top hits cohere
    # on one enumerable source, this pulls that source's COMPLETE ordered
    # chunk set so a list-of-techniques is surfaced whole, not clipped to the
    # top-5 snippets. Production wires ``default_expand_callable`` (the expand
    # backbone over the worker index); tests inject a fake returning a canned
    # ``ExpandOutput``.
    expand_fn: Callable[[str], ExpandOutput] = field(default_factory=lambda: default_expand_callable)


_GROUND_RULES = (
    "If the documents do not contain information about the topic, "
    'reply with exactly: "No relevant content found in the knowledge store." '
    "Do NOT fabricate, infer, or fill in plausible-sounding details. "
    "Do NOT add information that is not in the documents."
)

# #433 — when the LLM emits the canned no-relevance phrase the rest of
# the envelope must agree. Previously sources stayed populated even when
# the summary said "No relevant content found", producing the
# operator-visible contradiction "retrieval surfaced sources but
# synthesis says nothing matches". Detected via substring match against
# the canonical phrase (the prompt asks for it verbatim); the
# normalisation drops the sources list so the envelope is internally
# consistent.
_NO_RELEVANT_CANONICAL_PHRASE = "No relevant content found in the knowledge store"


def is_no_relevant_response(summary: str) -> bool:
    """Detect the canned no-relevance phrase in an LLM summary.

    Public so MCP / CLI callers can mirror the detection on their side
    without rewriting the substring check.
    """
    return _NO_RELEVANT_CANONICAL_PHRASE.lower() in (summary or "").lower()


def _build_messages(query: str, tier: str, context: str) -> list[dict[str, str]]:
    if tier == "l0":
        system = (
            "You are a concise knowledge assistant. Based ONLY on the provided documents, "
            "summarise what is known about the topic in 2-3 sentences. "
            f"{_GROUND_RULES}"
        )
    else:
        system = (
            "You are a knowledge assistant. Based ONLY on the provided documents, "
            "provide a structured overview of the topic. "
            f"{_GROUND_RULES}"
        )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": f"Topic: {query}\n\nDocuments:\n{context}"},
    ]


# Without this floor, a top-5 chunk hit with a 12-character snippet ("see
# ref-001") gets fed to the LLM as "context" — the model treats it as
# authoritative and hallucinates to fill the gap (#254 dogfood). 40 chars
# is empirical: a sentence-worth of grounding; anything shorter is
# title-equivalent. The floor is CHUNK-tier only — fact rows are
# structured triplets ("Caroline role: VP of People" ~ 27 chars) whose
# compactness is the feature, not a bug, and they get a dedicated
# minimum below (#327 Plan-B remediation D1).
_MIN_USEFUL_SNIPPET_CHARS = 40
_MIN_FACT_SNIPPET_CHARS = 1


@dataclass
class _ContextCounters:
    """Per-row counters accumulated by ``_classify_row`` for the trace log."""

    chunks_total: int = 0
    chunks_kept: int = 0
    facts_total: int = 0
    facts_kept: int = 0


def _classify_row(budgeted: Any, counters: _ContextCounters) -> tuple[SourceRef, str] | None:
    """Apply the per-tier snippet floor; return ``(source_ref, formatted_snippet)``
    or ``None`` when the row should be dropped from LLM context.

    PLA-274 / #437 — the first element is now a resolvable :class:`SourceRef`
    (built off the fused result's source_uri / path / page), not a bare
    title string, so prep emits citeable sources instead of unresolvable
    human titles. The LLM-context label still prefers the human title for
    readability, falling back to the path.

    Bumps the right counter on ``counters`` for both totals and kept rows
    — keeping the trace log accurate even when ``_format_context`` is
    extracted into helpers.
    """
    inner = getattr(budgeted, "result", None)
    if inner is None:
        return None
    path = str(getattr(inner, "path", "") or "")
    title = str(getattr(inner, "title", "") or "") or path
    snippet = (getattr(budgeted, "content", "") or "").strip()
    is_fact = path.startswith("facts://")
    if is_fact:
        counters.facts_total += 1
        floor = _MIN_FACT_SNIPPET_CHARS
    else:
        counters.chunks_total += 1
        floor = _MIN_USEFUL_SNIPPET_CHARS
    if len(snippet) < floor:
        return None
    if is_fact:
        counters.facts_kept += 1
    else:
        counters.chunks_kept += 1
    raw_page = getattr(inner, "source_page", None)
    ref = SourceRef.of(
        path=path,
        source_uri=str(getattr(inner, "source_uri", "") or ""),
        title=(title if title != path else None),
        collection=str(getattr(inner, "collection", "") or "") or None,
        source_page=int(raw_page) if isinstance(raw_page, int) else None,
    )
    return ref, f"[{title}]\n{snippet[:500]}"


def _format_context(search_result: Any) -> tuple[str, list[SourceRef], list[tuple[str, str]]]:
    """Project a SearchResult's top 5 hits into a context string + source refs.

    Chunk hits need ``_MIN_USEFUL_SNIPPET_CHARS`` of snippet content to
    earn LLM context inclusion — anything shorter is title-equivalent
    and feeds hallucination. Fact rows (synthesised under the
    ``facts://`` path by SearchPipeline's fact federation) carry
    intentionally compact entity-attribute-value triplets and are
    exempt from the chunk floor; they only need to be non-empty.

    Returns ``("", [], [])`` when no hit has usable snippet content — the
    caller treats this as "no relevant documents" rather than calling
    the LLM. The second element is a list of resolvable ``SourceRef``s
    (PLA-274 / #437), not human title strings. The third element is the
    parallel ``(source_uri, formatted_snippet)`` rows the caller feeds to
    source-cohesion enumeration completion (#437).

    Emits a single ``KAIRIX_TRACE``-gated INFO log capturing how many
    chunk vs fact hits were considered vs kept and the resulting LLM
    context size. Plan B-parity post-mortem (D4 remediation): this is
    the diagnostic that would have made D1 (fact snippets filtered by
    the chunk floor) obvious in seconds rather than two days.
    """
    parts: list[str] = []
    sources: list[SourceRef] = []
    rows: list[tuple[str, str]] = []
    counters = _ContextCounters()
    for budgeted in getattr(search_result, "results", [])[:5]:
        classified = _classify_row(budgeted, counters)
        if classified is None:
            continue
        ref, formatted = classified
        sources.append(ref)
        parts.append(formatted)
        rows.append((ref.source_uri, formatted))
    context = "\n\n---\n\n".join(parts) if parts else ""
    if trace_enabled():
        logger.info(
            "prep.context: chunks %d/%d kept, facts %d/%d kept, %d ctx chars",
            counters.chunks_kept,
            counters.chunks_total,
            counters.facts_kept,
            counters.facts_total,
            len(context),
        )
    return context, sources, rows


# Label for the completed-enumeration block spliced into the LLM context so
# the model reads it as the authoritative full list, not another snippet.
_ENUMERATION_BLOCK_LABEL = "Complete list from the source above (every item)"


def _with_completed_enumeration(
    context: str,
    rows: list[tuple[str, str]],
    expand_fn: Callable[[str], ExpandOutput],
) -> str:
    """Splice the dominant source's complete enumeration into ``context`` (#437).

    When the top hits cohere on one enumerable source, appends that source's
    full ordered content as a labelled block so the LLM enumerates every list
    item — not just the score-ranked top-5 snippets. A no-op (returns
    ``context`` unchanged) when no enumerable source dominates.
    """
    completed = complete_enumeration(rows, expand_fn=expand_fn)
    if completed is None:
        return context
    _uri, full_text = completed
    return f"{context}\n\n---\n\n[{_ENUMERATION_BLOCK_LABEL}]\n{full_text}"


def run_prep(
    query: str,
    *,
    agent: str | None = None,
    scope: Scope = Scope.SHARED_AGENT,
    tier: Literal["l0", "l1"] = "l0",
    deps: PrepDeps | None = None,
) -> PrepOutput:
    """Run grounded summarisation over retrieved documents.

    Never raises — failures populate ``PrepOutput.error``.

    Args:
        query: Topic to summarise.
        agent: Agent name for collection scoping.
        scope: Multi-agent scope (default shared+agent).
        tier: ``"l0"`` for 2-3 sentences, ``"l1"`` for structured overview.
        deps: Injectable dependencies; production callers leave None.
    """
    d = deps or PrepDeps()
    search = d.search_fn
    chat = d.chat_fn

    try:
        budget = _L0_BUDGET if tier == "l0" else _L1_BUDGET
        sr = search(query=query, agent=agent, scope=scope, budget=budget)
        context, sources, rows = _format_context(sr)

        if not context:
            return PrepOutput(
                query=query,
                tier=tier,
                summary="No relevant documents found for this topic.",
            )

        # #437 — when the top hits cohere on one enumerable source, complete
        # its enumeration so a list-of-techniques is surfaced whole.
        context = _with_completed_enumeration(context, rows, d.expand_fn)

        max_tokens = _L0_MAX_TOKENS if tier == "l0" else _L1_MAX_TOKENS
        messages = _build_messages(query, tier, context)

        # Cache-aside: identical ``(query, tier, retrieved-context)``
        # triples short-circuit the LLM call. Cache miss → call the
        # chat fn + store; cache hit → return the cached summary. The
        # cache key folds the context (via sha256) so callers asking
        # the same question over different retrieved-context blocks
        # never collide.
        cache = _get_or_create_prep_summary_cache()
        cache_key = make_prep_cache_key(query, tier, context)
        cached_summary = cache.get(cache_key)
        if cached_summary is not None:
            summary = cached_summary
        else:
            summary = chat(messages=messages, max_tokens=max_tokens)
            cache.put(cache_key, summary)

        # #433 — when the LLM emitted the canned no-relevance phrase,
        # drop the sources list so the envelope is internally consistent.
        # Pre-fix shape: summary="No relevant content found..." +
        # sources=[doc1, doc2, ...] — confusing for the operator.
        # Post-fix shape: summary="No relevant content found..." +
        # sources=[] — the two halves of the envelope agree.
        final_sources = [] if is_no_relevant_response(summary) else sources
        return PrepOutput(
            query=query,
            tier=tier,
            summary=summary,
            tokens=estimate_tokens(summary),
            sources=final_sources,
        )
    except Exception as exc:
        logger.warning("run_prep failed: %s", exc, exc_info=True)
        return PrepOutput(query=query, tier=tier, error=f"{type(exc).__name__}: {exc}")


def prep_output_to_envelope(out: PrepOutput) -> dict[str, Any]:
    """Project a ``PrepOutput`` to the JSON envelope MCP callers receive."""
    return {
        "query": out.query,
        "tier": out.tier,
        "summary": out.summary,
        "tokens": out.tokens,
        # PLA-274 / #437 — each source is a resolvable SourceRef breadcrumb.
        "sources": [s.to_envelope() for s in out.sources],
        "error": out.error,
    }
