"""Bootstrap use case — agent orientation envelope shared by CLI and MCP.

Issue #246, W1: a single call that returns the **orientation envelope**
an agent absorbs at session start. Pre-W1 agents started each session
context-blind — they had to invent a policy for when to call kairix
tools. ``run_bootstrap`` collapses "what's my role / board / recent
memory / health" into one structured response so the agent can absorb
its current state in one shot.

Design principle (#246): every kairix output should give the agent
maximum affordance for the next step. ``BootstrapOutput`` is the
canonical example: even when health is degraded, the envelope still
returns ``board`` and ``recent_memory`` (BM25-only paths), and the
``next_action`` field tells the agent what to do **right now** instead
of leaving it to guess.

W3 promoted the health probe + dataclass into ``kairix.core.health``
so every tool response shares one shape. ``BootstrapHealth`` survives
here as a back-compat alias for ``KairixHealth``; the probe defers to
``kairix.core.health.probe_health`` so bootstrap and every other tool
read the same snapshot.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kairix.core.health import (
    HEALTH_PROBE_BUDGET_S,
    HealthDeps,
    KairixHealth,
    health_to_envelope,
    probe_health,
)

logger = logging.getLogger(__name__)

# Back-compat alias: W1 callers (and the test suite) import
# ``BootstrapHealth`` from this module. W3 made the dataclass shared,
# so ``BootstrapHealth`` is now ``KairixHealth`` under a friendly name.
BootstrapHealth = KairixHealth

# Re-export the budget so callers that imported it from here keep working.
__all__ = [
    "HEALTH_PROBE_BUDGET_S",
    "BootstrapDeps",
    "BootstrapHealth",
    "BootstrapOutput",
    "KairixHealth",
    "MemoryEntry",
    "bootstrap_health_to_envelope",
    "bootstrap_output_to_envelope",
    "bootstrap_output_to_markdown",
    "run_bootstrap",
]


# ---------------------------------------------------------------------------
# Default helpers — production wiring for the dependency factories below
# ---------------------------------------------------------------------------


def _default_document_root() -> Path:
    """Resolve the document root via ``kairix.paths`` (F4-clean).

    Reads ``KAIRIX_DOCUMENT_ROOT`` through ``paths.document_root()`` so
    bootstrap never opens an env-read seam of its own.
    """
    from kairix.paths import document_root

    return document_root()


# ---------------------------------------------------------------------------
# Output dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MemoryEntry:
    """One daily memory file rendered for the bootstrap envelope."""

    date: str
    content: str


@dataclass(frozen=True)
class BootstrapOutput:
    """Outcome of one ``run_bootstrap`` invocation.

    Never raises — failures populate ``error`` and ``health`` rather
    than throwing. Even on partial failure the envelope keeps every
    field the agent expects so JSON consumers see a stable shape.

    Attributes:
        agent: The agent name passed in (verbatim — not normalised).
        role: Agent identity from the kairix profile, or empty string
            when no profile is present.
        board: Latest ``Board.md`` markdown body for the agent;
            empty string when the file is missing.
        recent_memory: Up to ``max_memory_days`` daily memory entries,
            newest first. Empty list when none exist or when
            ``max_memory_days=0``.
        active_goals: Bullet-style strings extracted from ``Goals.md``;
            empty list when the file is missing.
        health: Capability snapshot — see :class:`BootstrapHealth`.
        next_action: Prescriptive directive for the agent's first step.
            Mirrors ``health.next_action`` when degraded; falls back to
            "Read your Board for current priorities" on full success.
        error: Empty on success; populated when bootstrap itself failed
            (e.g. the document root does not exist). Separate from
            ``health.degraded_reason`` — health describes runtime
            capability; ``error`` describes bootstrap failure.
    """

    agent: str
    role: str = ""
    board: str = ""
    recent_memory: list[MemoryEntry] = field(default_factory=list)
    active_goals: list[str] = field(default_factory=list)
    health: BootstrapHealth = field(default_factory=BootstrapHealth)
    next_action: str = ""
    error: str = ""


# ---------------------------------------------------------------------------
# Dependency injection seam
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BootstrapDeps:
    """Injectable dependencies for ``run_bootstrap``.

    Mirrors the ``BriefDeps`` / ``UsageGuideDeps`` pattern: every field
    is non-Optional with a ``field(default_factory=...)`` so tests
    construct ``BootstrapDeps(document_root_fn=fake, ...)`` and
    production callers leave ``deps=None`` — the defaults wire the real
    helpers via lazy import.

    F6-clean: no test-only kwargs leak into ``run_bootstrap``; all
    seams flow through this dataclass. The three health probes are
    surfaced as flat fields here for back-compat with W1 callers; they
    project onto a ``HealthDeps`` for the shared probe below.
    """

    document_root_fn: Callable[[], Path] = field(default_factory=lambda: _default_document_root)
    secrets_loaded_fn: Callable[[], bool] = field(default_factory=lambda: HealthDeps().secrets_loaded_fn)
    embed_backend_available_fn: Callable[[], bool] = field(
        default_factory=lambda: HealthDeps().embed_backend_available_fn
    )
    bm25_index_available_fn: Callable[[], bool] = field(default_factory=lambda: HealthDeps().bm25_index_available_fn)


# ---------------------------------------------------------------------------
# Health probe — delegates to kairix.core.health
# ---------------------------------------------------------------------------


def _probe_health(deps: BootstrapDeps) -> KairixHealth:
    """Compose the ``KairixHealth`` snapshot from the injectable probes.

    Bootstrap takes the flat W1 fields on ``BootstrapDeps`` and forwards
    them to ``kairix.core.health.probe_health`` so every tool reads the
    same snapshot. The probe honours the 2s budget and never raises.
    """
    return probe_health(
        HealthDeps(
            secrets_loaded_fn=deps.secrets_loaded_fn,
            embed_backend_available_fn=deps.embed_backend_available_fn,
            bm25_index_available_fn=deps.bm25_index_available_fn,
        )
    )


# ---------------------------------------------------------------------------
# Vault loaders — pure file readers, never raise
# ---------------------------------------------------------------------------


def _agent_dir(root: Path, agent: str) -> Path:
    """Resolve ``${VAULT_ROOT}/04-Agent-Knowledge/<agent>/`` deterministically."""
    return root / "04-Agent-Knowledge" / agent


def _read_text(path: Path) -> str:
    """Read ``path`` as UTF-8; empty string on any failure."""
    try:
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("bootstrap _read_text failed for %s: %s", path, exc)
        return ""


def _load_board(agent_dir: Path) -> str:
    """Latest ``Board.md`` content; empty string when missing."""
    return _read_text(agent_dir / "Board.md")


def _load_goals(agent_dir: Path) -> list[str]:
    """Extract bullet-style active goals from ``Goals.md``.

    Returns the leading bullet lines (``- ...``, ``* ...``, ``1. ...``)
    with their markers stripped. Falls back to non-empty paragraph
    lines when no bullets are present. Empty list when the file is
    missing or unreadable.
    """
    text = _read_text(agent_dir / "Goals.md")
    if not text:
        return []

    goals: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        if line.startswith(("- ", "* ")):
            goals.append(line[2:].strip())
            continue
        # Numbered list item: "1. ..."
        if len(line) >= 3 and line[0].isdigit() and line[1:].startswith(". "):
            goals.append(line.split(". ", 1)[1].strip())
            continue

    if goals:
        return goals

    # No bullets found — fall through to non-empty plain lines so the
    # agent still gets *something* useful when goals are written as
    # prose. Strip leading markdown emphasis on the first chars only.
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def _load_recent_memory(agent_dir: Path, *, max_days: int) -> list[MemoryEntry]:
    """Read up to ``max_days`` newest ``memory/YYYY-MM-DD.md`` files.

    The memory directory follows the project's daily-file convention.
    Files are sorted by name (ISO-8601 dates sort lexicographically =
    chronologically) and the newest ``max_days`` are returned newest
    first. Returns ``[]`` when ``max_days <= 0`` so callers can ask for
    "no memory" cleanly.
    """
    if max_days <= 0:
        return []

    memory_dir = agent_dir / "memory"
    if not memory_dir.is_dir():
        return []

    try:
        candidates = sorted(
            (p for p in memory_dir.iterdir() if p.is_file() and p.suffix == ".md"),
            key=lambda p: p.name,
            reverse=True,
        )
    except OSError as exc:
        logger.warning("bootstrap _load_recent_memory listing failed: %s", exc)
        return []

    entries: list[MemoryEntry] = []
    for path in candidates[:max_days]:
        date_str = path.stem  # YYYY-MM-DD when the file is well-formed
        content = _read_text(path)
        entries.append(MemoryEntry(date=date_str, content=content))
    return entries


def _load_role(agent_dir: Path) -> str:
    """Read the agent's role line from ``profile.md`` or ``Role.md``.

    Tries the most-explicit filename first. Returns the first non-blank
    line stripped of leading ``#`` characters. Empty string when neither
    file exists — the bootstrap envelope reports ``role=""`` and the
    agent can still proceed.
    """
    for filename in ("profile.md", "Role.md"):
        text = _read_text(agent_dir / filename)
        if not text:
            continue
        for raw_line in text.splitlines():
            stripped = raw_line.strip().lstrip("#").strip()
            if stripped:
                return stripped
    return ""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_bootstrap(
    agent: str,
    *,
    deps: BootstrapDeps | None = None,
    max_memory_days: int = 3,
) -> BootstrapOutput:
    """Produce the orientation envelope for ``agent``.

    Never raises — failures populate ``BootstrapOutput.error`` and the
    health probe degrades gracefully. The envelope **always** returns
    ``board``, ``recent_memory``, ``active_goals`` whenever the agent
    directory exists, even if vector search is offline; that's the W1
    affordance contract.

    Args:
        agent: Agent name (used verbatim as the directory slug).
        deps: Injectable dependencies; production callers leave None.
        max_memory_days: Number of newest daily memory files to include.
            ``0`` returns an empty list with no error.
    """
    d = deps or BootstrapDeps()

    try:
        root = d.document_root_fn()
    except Exception as exc:
        logger.warning("run_bootstrap document_root_fn failed: %s", exc, exc_info=True)
        return BootstrapOutput(
            agent=agent,
            health=_probe_health(d),
            next_action=(
                "Configure KAIRIX_DOCUMENT_ROOT or ask your admin to set the document root. "
                "Run 'kairix onboard check' to diagnose."
            ),
            error=f"{type(exc).__name__}: {exc}",
        )

    if not root.exists():
        health = _probe_health(d)
        return BootstrapOutput(
            agent=agent,
            health=health,
            next_action=(
                "Configure KAIRIX_DOCUMENT_ROOT or ask your admin — the document root does not exist. "
                "Run 'kairix onboard check' to diagnose."
            ),
            error=f"DocumentRootMissing: {root}",
        )

    agent_dir = _agent_dir(root, agent)
    # Even when the agent dir doesn't exist we still return a valid
    # envelope — the agent might be brand new. Each loader returns its
    # empty form, and the next_action below tells the agent what to do.

    board = _load_board(agent_dir)
    goals = _load_goals(agent_dir)
    memory = _load_recent_memory(agent_dir, max_days=max_memory_days)
    role = _load_role(agent_dir)
    health = _probe_health(d)

    next_action = _pick_next_action(
        agent_dir_exists=agent_dir.is_dir(),
        board=board,
        health=health,
    )

    return BootstrapOutput(
        agent=agent,
        role=role,
        board=board,
        recent_memory=memory,
        active_goals=goals,
        health=health,
        next_action=next_action,
    )


def _pick_next_action(*, agent_dir_exists: bool, board: str, health: BootstrapHealth) -> str:
    """Choose the agent-facing prescriptive directive for the envelope."""
    if health.next_action:
        return health.next_action
    if not agent_dir_exists:
        return (
            "No agent profile found yet — ask your admin to scaffold "
            "${KAIRIX_DOCUMENT_ROOT}/04-Agent-Knowledge/<agent>/Board.md."
        )
    if board:
        return "Read your Board for current priorities, then call tool_search before answering factual questions."
    return (
        "Your Board is empty — ask your human what to prioritise, then call tool_search "
        "before answering factual questions."
    )


# ---------------------------------------------------------------------------
# Envelope projection
# ---------------------------------------------------------------------------


def bootstrap_health_to_envelope(health: BootstrapHealth) -> dict[str, Any]:
    """Project a ``BootstrapHealth`` (aka ``KairixHealth``) to the JSON dict.

    Back-compat wrapper around ``kairix.core.health.health_to_envelope``
    for W1 callers; the return shape is identical.
    """
    return dict(health_to_envelope(health))


def bootstrap_output_to_envelope(out: BootstrapOutput) -> dict[str, Any]:
    """Project a ``BootstrapOutput`` to the JSON envelope MCP callers receive."""
    return {
        "agent": out.agent,
        "role": out.role,
        "board": out.board,
        "recent_memory": [{"date": m.date, "content": m.content} for m in out.recent_memory],
        "active_goals": list(out.active_goals),
        "health": bootstrap_health_to_envelope(out.health),
        "next_action": out.next_action,
        "error": out.error,
    }


def bootstrap_output_to_markdown(out: BootstrapOutput) -> str:
    """Render a ``BootstrapOutput`` as the CLI-facing markdown document.

    The CLI surface mirrors the JSON envelope structure so operators and
    agents see the same fields in the same order; the only difference
    is presentation.
    """
    lines: list[str] = [f"# Bootstrap envelope: {out.agent}"]
    if out.role:
        lines.append(f"\n**Role:** {out.role}")
    if out.error:
        lines.append(f"\n**Error:** {out.error}")
    lines.append("")
    lines.append("## Health")
    lines.append(f"- vector_search: {out.health.vector_search}")
    lines.append(f"- bm25: {out.health.bm25}")
    lines.append(f"- chat: {out.health.chat}")
    lines.append(f"- secrets_loaded: {out.health.secrets_loaded}")
    if out.health.degraded_reason:
        lines.append(f"- degraded_reason: {out.health.degraded_reason}")
    lines.append("")
    lines.append("## Next action")
    lines.append(out.next_action or "(none)")
    lines.append("")
    lines.append("## Board")
    lines.append(out.board if out.board else "_(no Board.md found)_")
    lines.append("")
    lines.append("## Active goals")
    if out.active_goals:
        for g in out.active_goals:
            lines.append(f"- {g}")
    else:
        lines.append("_(no Goals.md found)_")
    lines.append("")
    lines.append("## Recent memory")
    if out.recent_memory:
        for entry in out.recent_memory:
            lines.append(f"### {entry.date}")
            lines.append(entry.content)
            lines.append("")
    else:
        lines.append("_(no recent memory entries)_")
    return "\n".join(lines).rstrip() + "\n"
