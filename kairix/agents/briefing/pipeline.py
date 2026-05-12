"""
8-step briefing pipeline for kairix session briefings.

Steps:
  1. Recent memory log files (last 7 days, tagged items)
  2. Today's + yesterday's memory file (full content)
  3. Entity stub for agent
  4. Agent knowledge rules
  5. Recent decisions (last 30 days)
  6. Hybrid search on agent name
  7. GPT-4o-mini synthesis
  8. Write to /data/kairix/briefing/<agent>-latest.md

Steps 1-6 run concurrently. Total context is capped at 3000 tokens with
priority-based truncation (step 6 first, then 5, 4, etc.).

Never raises — returns partial briefing on any failure.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from kairix.text import estimate_tokens, truncate_to_tokens

logger = logging.getLogger(__name__)


def _default_synthesise() -> Callable[..., str]:
    """Return the production synthesiser. Lazy import breaks the module cycle."""
    from kairix.agents.briefing.synthesiser import synthesise

    return synthesise


def _default_write_briefing() -> Callable[..., Path]:
    """Return the production writer. Lazy import breaks the module cycle."""
    from kairix.agents.briefing.writer import write_briefing

    return write_briefing


@dataclass
class BriefingDeps:
    """Injectable dependencies for ``generate_briefing``.

    Each field defaults to the production implementation via
    ``default_factory`` — fields are typed as concrete callables so mypy
    sees a real type at every call site (no ``assert deps.x is not None``
    ladder). Tests construct ``BriefingDeps(synthesise_fn=fake, ...)`` to
    swap individual collaborators.
    """

    synthesise_fn: Callable[..., str] = field(default_factory=_default_synthesise)
    write_fn: Callable[..., Path] = field(default_factory=_default_write_briefing)


# Token caps per source (approximate)
_SOURCE_TOKEN_CAPS: dict[str, int] = {
    "memory_logs": 500,
    "recent_memory": 300,
    "entity_stub": 400,
    "knowledge_rules": 300,
    "recent_decisions": 400,
    "hybrid_search": 600,
}

# Total context budget before truncation (3000 tokens ~ 2300 words)
_TOTAL_CONTEXT_CAP = 3000

# Priority order for truncation when over budget (lowest priority first)
_TRUNCATION_ORDER = [
    "hybrid_search",
    "recent_decisions",
    "knowledge_rules",
    "entity_stub",
    "recent_memory",
    "memory_logs",
]


def _run_source(name: str, fn, *args) -> tuple[str, str]:
    """
    Run a source fetcher safely. Returns (name, content).
    Logs warning and returns empty string on any failure.
    """
    try:
        result = fn(*args)
        return name, result or ""
    except Exception as e:
        logger.warning("pipeline: source %r failed — %s", name, e)
        return name, ""


def _trim_context(context: dict[str, str]) -> dict[str, str]:
    """
    Trim context sources if total token estimate exceeds _TOTAL_CONTEXT_CAP.
    Truncates lowest-priority sources first.
    """
    total = sum(estimate_tokens(v) for v in context.values())
    if total <= _TOTAL_CONTEXT_CAP:
        return context

    trimmed = dict(context)
    for source_name in _TRUNCATION_ORDER:
        if total <= _TOTAL_CONTEXT_CAP:
            break
        if trimmed.get(source_name):
            current = trimmed[source_name]
            current_tokens = estimate_tokens(current)
            cap = _SOURCE_TOKEN_CAPS.get(source_name, 200)
            if current_tokens > cap // 2:
                # Halve the allocation
                new_cap = max(cap // 2, 50)
                trimmed[source_name] = truncate_to_tokens(current, new_cap)
                total -= current_tokens - estimate_tokens(trimmed[source_name])

    return trimmed


def generate_briefing(
    agent: str,
    *,
    deps: BriefingDeps | None = None,
    sources: dict[str, Callable] | None = None,
    output_dir: Path | None = None,
) -> str:
    """
    Generate a session briefing for the given agent.

    Runs the full 8-step pipeline:
    1-6: Concurrent source fetching
    7:   GPT-4o-mini synthesis
    8:   Write to file

    Args:
        agent:      Agent name (e.g. "builder", "shape").
        deps:       Injectable dependencies (synthesise_fn, write_fn).
                    Production callers leave None — the dataclass wires
                    real implementations via ``default_factory``. Tests
                    construct ``BriefingDeps(synthesise_fn=fake, ...)``.
        sources:    Per-source callable overrides (key = source name).
        output_dir: Optional output directory override (currently unused
                    by this function; reserved for future).

    Returns:
        Full briefing content (with header). Never raises.
    """
    d = deps or BriefingDeps()
    synthesise = d.synthesise_fn
    write_briefing = d.write_fn

    t_start = time.monotonic()
    logger.info("pipeline: generating briefing for agent %r", agent)

    # Resolve source fetchers — allow per-source overrides via the `sources` dict
    _src = sources or {}

    def _resolve_source(name: str, default_import_path: str) -> Callable:
        if name in _src:
            return _src[name]
        # Lazy import from default module
        from kairix.agents.briefing import sources as _sources_mod

        return getattr(_sources_mod, default_import_path)

    _fetch_memory_logs = _resolve_source("memory_logs", "fetch_memory_logs")
    _fetch_recent_memory = _resolve_source("recent_memory", "fetch_recent_memory")
    _fetch_entity_stub = _resolve_source("entity_stub", "fetch_entity_stub")
    _fetch_knowledge_rules = _resolve_source("knowledge_rules", "fetch_knowledge_rules")
    _fetch_recent_decisions = _resolve_source("recent_decisions", "fetch_recent_decisions")
    _fetch_hybrid_search = _resolve_source("hybrid_search", "fetch_hybrid_search")

    # Steps 1-6: concurrent source fetching
    source_tasks = [
        ("memory_logs", _fetch_memory_logs, agent, _SOURCE_TOKEN_CAPS["memory_logs"]),
        (
            "recent_memory",
            _fetch_recent_memory,
            agent,
            _SOURCE_TOKEN_CAPS["recent_memory"],
        ),
        ("entity_stub", _fetch_entity_stub, agent, _SOURCE_TOKEN_CAPS["entity_stub"]),
        (
            "knowledge_rules",
            _fetch_knowledge_rules,
            agent,
            _SOURCE_TOKEN_CAPS["knowledge_rules"],
        ),
        (
            "recent_decisions",
            _fetch_recent_decisions,
            agent,
            _SOURCE_TOKEN_CAPS["recent_decisions"],
        ),
        (
            "hybrid_search",
            _fetch_hybrid_search,
            agent,
            _SOURCE_TOKEN_CAPS["hybrid_search"],
        ),
    ]

    context: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=6) as executor:
        future_map: dict[Future, str] = {}
        for name, fn, *args in source_tasks:
            future = executor.submit(_run_source, name, fn, *args)
            future_map[future] = name

        for future in as_completed(future_map, timeout=25):
            try:
                source_name, content = future.result()
                if content:
                    context[source_name] = content
                    logger.debug(
                        "pipeline: source %r returned %d tokens",
                        source_name,
                        estimate_tokens(content),
                    )
            except Exception as e:
                name = future_map[future]
                logger.warning("pipeline: source %r future failed — %s", name, e)

    sources_count = len(context)
    logger.info("pipeline: collected %d sources for %r", sources_count, agent)

    # Surface missing memory — helps users diagnose stale briefings
    memory_keys = {"memory_logs", "recent_memory"}
    if not (memory_keys & context.keys()):
        from kairix.paths import agent_memory_path

        mem_path = agent_memory_path(agent)
        context["_missing_memory_note"] = (
            f"No agent memory logs found at {mem_path}. "
            f"Briefing is based on knowledge store and entity data only. "
            f"To enable memory-based briefing, create daily log files at "
            f"{mem_path}/YYYY-MM-DD.md"
        )
        logger.warning(
            "pipeline: no memory sources for agent %r — briefing may be stale",
            agent,
        )

    # Trim context if over budget
    context = _trim_context(context)

    # Step 7: Synthesise
    briefing_body = synthesise(agent, context, max_tokens=800)

    # Token estimate for output
    token_estimate = estimate_tokens(briefing_body)

    # Step 8: Write to file
    try:
        out_path = write_briefing(
            agent=agent,
            content=briefing_body,
            sources_count=sources_count,
            token_estimate=token_estimate,
        )
        logger.info(
            "pipeline: briefing written to %s in %.1fs",
            out_path,
            time.monotonic() - t_start,
        )
    except OSError as e:
        logger.error("pipeline: could not write briefing file — %s", e)
        # Return the content anyway

    # Read back what was written (includes header added by writer)
    try:
        from kairix.agents.briefing.writer import BRIEFING_DIR

        out_path = BRIEFING_DIR / f"{agent}-latest.md"
        if out_path.exists():
            return out_path.read_text(encoding="utf-8")
    except Exception as _exc:
        logger.debug("pipeline: could not read back briefing file — %s", _exc)

    # Fallback: build content inline
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%d %H:%M UTC")
    date_str = now.strftime("%Y-%m-%d")
    header = (
        f"# Agent Briefing — {agent} — {date_str}\n"
        f"_Generated: {ts} | Sources: {sources_count} | Tokens: ~{token_estimate}_\n\n"
    )
    return header + briefing_body


# ---------------------------------------------------------------------------
# BriefingPipeline class — composable orchestrator
# ---------------------------------------------------------------------------


@dataclass
class BriefingPipeline:
    """Composable briefing orchestrator.

    Wraps the procedural generate_briefing() in a dataclass so callers
    can construct it once with injected dependencies and call generate()
    for each agent.

    Attributes:
        sources:    Per-source callable overrides (key = source name).
        deps:       Injectable dependencies (synthesise_fn, write_fn).
                    Defaults to production implementations.
        output_dir: Optional output directory override (unused by
                    generate_briefing today, reserved for future).
    """

    sources: dict[str, Callable] = field(default_factory=dict)
    deps: BriefingDeps = field(default_factory=BriefingDeps)
    output_dir: Path | None = None

    def generate(self, agent: str) -> str:
        """Generate a session briefing for the given agent.

        Delegates to the procedural generate_briefing() with the
        configured dependencies.

        Args:
            agent: Agent name (e.g. "builder", "shape").

        Returns:
            Full briefing content string. Never raises.
        """
        return generate_briefing(
            agent=agent,
            deps=self.deps,
            sources=self.sources if self.sources else None,
            output_dir=self.output_dir,
        )
