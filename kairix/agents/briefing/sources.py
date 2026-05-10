"""
Individual source fetchers for the briefing pipeline.

Each fetcher is independent and safe to run concurrently.
All functions return strings (may be empty on failure) and never raise.

Each fetcher accepts an optional ``memory_dir`` / ``document_root`` Path
override so tests can pass a tmp_path-rooted layout without monkeypatching
the kairix.paths helpers. Production callers leave them ``None`` and the
helpers resolve via ``kairix.paths``.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

from kairix.text import truncate_to_tokens

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Source 1: Recent memory log files (last 7 days)
# ---------------------------------------------------------------------------


def fetch_memory_logs(agent: str, max_tokens: int = 500, memory_dir: Path | None = None) -> str:
    """
    Fetch last 7 days of memory log files for agent.

    Extracts items tagged [pending], [blocked], [action:], and summaries.
    Returns empty string on failure.
    """
    try:
        if memory_dir is None:
            from kairix.paths import agent_memory_path

            memory_dir = agent_memory_path(agent)
        if not memory_dir.exists():
            logger.warning(
                "sources: memory dir not found for agent %r at %s — create it with: mkdir -p %s",
                agent,
                memory_dir,
                memory_dir,
            )
            return ""

        today = date.today()
        lines: list[str] = []

        for days_back in range(7):
            day = today - timedelta(days=days_back)
            path = memory_dir / f"{day.isoformat()}.md"
            if not path.exists():
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
                # Extract tagged items and session headers
                for line in content.splitlines():
                    stripped = line.strip()
                    if any(tag in stripped.lower() for tag in ["[pending]", "[blocked]", "[action:", "todo", "## "]):
                        lines.append(f"[{day.isoformat()}] {stripped}")
            except Exception as e:
                logger.warning("sources: error reading memory log %s — %s", path, e)

        if not lines:
            return ""

        result = "\n".join(lines)
        return truncate_to_tokens(result, max_tokens)

    except Exception as e:
        logger.warning("sources: fetch_memory_logs failed for %r — %s", agent, e)
        return ""


# ---------------------------------------------------------------------------
# Source 2: Today's + yesterday's memory files (full content)
# ---------------------------------------------------------------------------


def fetch_recent_memory(agent: str, max_tokens: int = 300, memory_dir: Path | None = None) -> str:
    """
    Fetch today's and yesterday's memory files for agent (full content).
    Returns empty string on failure.
    """
    try:
        if memory_dir is None:
            from kairix.paths import agent_memory_path

            memory_dir = agent_memory_path(agent)
        if not memory_dir.exists():
            return ""

        today = date.today()
        yesterday = today - timedelta(days=1)

        parts: list[str] = []
        for day in [today, yesterday]:
            path = memory_dir / f"{day.isoformat()}.md"
            if path.exists():
                try:
                    content = path.read_text(encoding="utf-8", errors="replace")
                    parts.append(f"### {day.isoformat()}\n{content}")
                except Exception as e:
                    logger.warning("sources: error reading %s — %s", path, e)

        if not parts:
            return ""

        combined = "\n\n".join(parts)
        return truncate_to_tokens(combined, max_tokens)

    except Exception as e:
        logger.warning("sources: fetch_recent_memory failed for %r — %s", agent, e)
        return ""


# ---------------------------------------------------------------------------
# Source 3: Entity stub for agent
# ---------------------------------------------------------------------------


def _resolve_document_root(document_root: Path | None) -> Path:
    if document_root is not None:
        return document_root
    from kairix.paths import document_root as _document_root

    return _document_root()


def fetch_entity_stub(agent: str, max_tokens: int = 400, document_root: Path | None = None) -> str:
    """
    Fetch the agent's own entity stub from vault-entities.
    Returns empty string on failure.
    """
    try:
        root = _resolve_document_root(document_root)
        # Try agent-specific entity stub (concept type)
        candidate_paths = [
            root / "04-Agent-Knowledge" / "entities" / "concept" / f"{agent}.md",
            root / "04-Agent-Knowledge" / "entities" / "agent" / f"{agent}.md",
            root / "04-Agent-Knowledge" / "entities" / "person" / f"{agent}.md",
        ]

        for path in candidate_paths:
            if path.exists():
                try:
                    content = path.read_text(encoding="utf-8", errors="replace")
                    return truncate_to_tokens(content, max_tokens)
                except Exception as e:
                    logger.warning("sources: error reading entity stub %s — %s", path, e)

        logger.debug("sources: no entity stub found for agent %r", agent)
        return ""

    except Exception as e:
        logger.warning("sources: fetch_entity_stub failed for %r — %s", agent, e)
        return ""


# ---------------------------------------------------------------------------
# Source 4: Agent knowledge rules
# ---------------------------------------------------------------------------


def fetch_knowledge_rules(agent: str, max_tokens: int = 300, document_root: Path | None = None) -> str:
    """
    Fetch rules/constraints from agent's knowledge collection.
    Returns empty string on failure.
    """
    try:
        root = _resolve_document_root(document_root)
        rules_paths = [
            root / "04-Agent-Knowledge" / agent / "rules.md",
            root / "04-Agent-Knowledge" / "shared" / "rules.md",
        ]

        parts: list[str] = []
        for path in rules_paths:
            if path.exists():
                try:
                    content = path.read_text(encoding="utf-8", errors="replace")
                    parts.append(f"### Rules from {path.parent.name}/rules.md\n{content}")
                except Exception as e:
                    logger.warning("sources: error reading rules %s — %s", path, e)

        if not parts:
            return ""

        combined = "\n\n".join(parts)
        return truncate_to_tokens(combined, max_tokens)

    except Exception as e:
        logger.warning("sources: fetch_knowledge_rules failed for %r — %s", agent, e)
        return ""


# ---------------------------------------------------------------------------
# Source 5: Recent decisions (last 30 days)
# ---------------------------------------------------------------------------


def fetch_recent_decisions(agent: str, max_tokens: int = 400, document_root: Path | None = None) -> str:
    """
    Fetch decisions from last 30 days from decisions.md.
    Returns empty string on failure.
    """
    try:
        root = _resolve_document_root(document_root)
        parts: list[str] = []

        # decisions.md
        decisions_path = root / "04-Agent-Knowledge" / agent / "decisions.md"
        if decisions_path.exists():
            try:
                content = decisions_path.read_text(encoding="utf-8", errors="replace")
                # Take last 30 days worth — heuristic: last 3000 chars
                if len(content) > 3000:
                    content = content[-3000:]
                parts.append(f"### decisions.md\n{content}")
            except Exception as e:
                logger.warning("sources: error reading decisions.md — %s", e)

        if not parts:
            return ""

        combined = "\n\n".join(parts)
        return truncate_to_tokens(combined, max_tokens)

    except Exception as e:
        logger.warning("sources: fetch_recent_decisions failed for %r — %s", agent, e)
        return ""


# ---------------------------------------------------------------------------
# Source 6: Hybrid search on agent name
# ---------------------------------------------------------------------------


def fetch_hybrid_search(agent: str, max_tokens: int = 600) -> str:
    """
    Run hybrid search on agent name to get top 5 relevant chunks.
    Returns empty string on failure.
    """
    try:
        from kairix.core.factory import build_search_pipeline

        _pipeline = build_search_pipeline()
        result = _pipeline.search(query=agent, agent=agent, scope="shared+agent", budget=max_tokens * 2)

        if not result.results:
            return ""

        chunks: list[str] = []
        for item in result.results[:5]:
            path = getattr(item.result, "path", "unknown")
            content = getattr(item, "content", "")[:400]
            chunks.append(f"**{path}**\n{content}")

        combined = "\n\n---\n\n".join(chunks)
        return truncate_to_tokens(combined, max_tokens)

    except Exception as e:
        logger.warning("sources: fetch_hybrid_search failed for %r — %s", agent, e)
        return ""
