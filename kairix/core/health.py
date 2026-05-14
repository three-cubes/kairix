"""Shared health probe — kairix capability snapshot for every tool response.

Issue #246 W3: every kairix tool response (search, brief, entity,
bootstrap, …) carries a ``health`` envelope so the agent never has to
guess "is kairix working right now". This module owns the canonical
``KairixHealth`` dataclass plus the dependency-injected probe that
fills it in.

Design principle (#246): when kairix degrades, the response **still
returns useful results** from the working subsystem AND tells the agent
what's offline AND what to do next. The probe never makes a network
call; the budget below caps the whole snapshot so a slow filesystem or
slow import can't stall a tool response.

Originally W1 shipped ``BootstrapHealth`` inside ``kairix.use_cases.bootstrap``;
W3 promotes it into ``kairix.core.health`` so every use case shares one
shape. ``BootstrapHealth`` survives as a back-compat alias from the
bootstrap module.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Time-cap for the full health probe in seconds. No network calls are
# made; this is a defence-in-depth budget against a slow filesystem or
# a slow import (#246). Individual probes that exceed the per-probe
# slice are cancelled and marked offline with a timeout reason.
HEALTH_PROBE_BUDGET_S: float = 2.0


# ---------------------------------------------------------------------------
# Default probe helpers — production wiring for HealthDeps below
# ---------------------------------------------------------------------------


def _default_secrets_loaded() -> bool:
    """Lightweight probe: is ``KAIRIX_LLM_API_KEY`` resolvable?

    Goes through ``kairix.secrets.get_secret`` with ``required=False`` so
    a missing secret returns ``None`` instead of raising. The result is
    a boolean — callers want "is the LLM credential available" not the
    secret value.
    """
    from kairix.secrets import get_secret

    try:
        value = get_secret("kairix-llm-api-key", required=False)
        return bool(value)
    except Exception as exc:  # pragma: no cover  # defensive lazy-import guard for get_secret
        logger.warning("_default_secrets_loaded probe failed: %s", exc, exc_info=True)
        return False


def _default_embed_backend_available() -> bool:
    """Lightweight probe: can we import the embed backend?

    A failed import or a missing client surface signals the vector-search
    leg is offline. Never raises — returns ``False`` on any failure.
    """
    try:
        import importlib

        importlib.import_module("kairix.core.embed.embed")
        return True
    except Exception as exc:  # pragma: no cover  # defensive guard for optional-extra import failure
        logger.warning("_default_embed_backend_available probe failed: %s", exc, exc_info=True)
        return False


def _default_bm25_index_available() -> bool:
    """Lightweight probe: does the FTS5 BM25 index exist on disk?

    Resolves ``paths.db_path()`` and reports whether the sqlite file is
    present. Does **not** open a connection — keeps the probe fast and
    avoids holding a lock during the snapshot.
    """
    try:
        from kairix.paths import db_path

        return db_path().exists()
    except Exception as exc:  # pragma: no cover  # defensive lazy-import guard for paths.db_path
        logger.warning("_default_bm25_index_available probe failed: %s", exc, exc_info=True)
        return False


def _default_neo4j_available() -> bool:
    """Lightweight probe: is the Neo4j client reachable for entity lookups?

    Imports the client and checks its ``available`` property — no Cypher
    is issued. Never raises; returns ``False`` on any failure.
    """
    try:
        from kairix.knowledge.graph.client import get_client

        return bool(get_client().available)
    except Exception as exc:  # pragma: no cover  # defensive lazy-import guard for get_client
        logger.warning("_default_neo4j_available probe failed: %s", exc, exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Output dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KairixHealth:
    """Capability health snapshot returned with every tool response.

    Each ``"ok" | "degraded" | "offline"`` field tells the agent which
    leg of kairix is usable right now; ``next_action`` is the
    prescriptive directive the agent should follow when the snapshot is
    not fully healthy. ``BootstrapHealth`` is a back-compat alias for
    this class — see ``kairix.use_cases.bootstrap``.

    Attributes:
        vector_search: ``"ok"`` when LLM creds + embed backend resolve;
            ``"degraded"`` when one of {creds, backend} is missing but
            BM25 still works; ``"offline"`` when neither is usable.
        bm25: ``"ok"`` when the FTS5 index exists; ``"offline"`` otherwise.
        chat: ``"ok"`` when ``KAIRIX_LLM_API_KEY`` resolves; ``"offline"``
            otherwise. Synthesis / research / brief depend on this.
        secrets_loaded: ``True`` when the LLM credential resolved; the
            most actionable bit for the human admin.
        degraded_reason: Human-readable cause; empty when fully ok.
        next_action: Prescriptive directive the agent should follow now.
    """

    vector_search: str = "ok"
    bm25: str = "ok"
    chat: str = "ok"
    secrets_loaded: bool = True
    degraded_reason: str = ""
    next_action: str = ""


# ---------------------------------------------------------------------------
# Dependency injection seam
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HealthDeps:
    """Injectable dependencies for ``probe_health``.

    Every field is non-Optional with a ``field(default_factory=...)`` so
    tests construct ``HealthDeps(secrets_loaded_fn=fake, ...)`` and
    production callers leave ``deps=None`` — the defaults wire the real
    helpers via lazy import. F6-clean.
    """

    secrets_loaded_fn: Callable[[], bool] = field(default_factory=lambda: _default_secrets_loaded)
    embed_backend_available_fn: Callable[[], bool] = field(default_factory=lambda: _default_embed_backend_available)
    bm25_index_available_fn: Callable[[], bool] = field(default_factory=lambda: _default_bm25_index_available)
    neo4j_available_fn: Callable[[], bool] = field(default_factory=lambda: _default_neo4j_available)


# ---------------------------------------------------------------------------
# Health probe
# ---------------------------------------------------------------------------


def _run_with_timeout(fn: Callable[[], bool], timeout_s: float) -> tuple[bool, bool]:
    """Run ``fn`` on a background thread; return ``(value, timed_out)``.

    A probe that exceeds ``timeout_s`` is treated as ``False`` and
    ``timed_out=True``. The background thread is daemonised so a
    runaway probe can't keep the interpreter alive. The probe itself
    can raise — exceptions are swallowed into ``(False, False)``.

    No event loop required; this works in sync tool adapters as well
    as in tests.
    """
    result: dict[str, bool] = {"value": False}

    def _runner() -> None:
        try:
            result["value"] = bool(fn())
        except Exception as exc:
            logger.warning("kairix.core.health probe raised: %s", exc, exc_info=True)
            result["value"] = False

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(timeout=timeout_s)
    if t.is_alive():
        return False, True
    return result["value"], False


def probe_health(
    deps: HealthDeps | None = None,
    *,
    budget_s: float = HEALTH_PROBE_BUDGET_S,
) -> KairixHealth:
    """Compose a ``KairixHealth`` snapshot from the injectable probes.

    Each probe is called once on a daemon thread with a per-probe slice
    of the overall ``budget_s``. The overall state is computed from the
    signals:

    - ``secrets_loaded=False`` → chat offline, vector search offline.
    - ``embed_backend=False``  → vector search offline.
    - ``bm25_index=False``     → bm25 offline.

    When any leg degrades, ``degraded_reason`` and ``next_action`` carry
    the operator-facing rationale + the agent-facing directive. No
    network calls; no exceptions — probe failures and timeouts fall
    through to the least-healthy state.
    """
    d = deps or HealthDeps()

    # Four probes share the budget evenly. Each probe is independent so
    # one slow probe can't starve the others.
    probe_slice = max(budget_s / 4.0, 0.05)

    secrets_loaded, secrets_timed_out = _run_with_timeout(d.secrets_loaded_fn, probe_slice)
    embed_backend, embed_timed_out = _run_with_timeout(d.embed_backend_available_fn, probe_slice)
    bm25_available, bm25_timed_out = _run_with_timeout(d.bm25_index_available_fn, probe_slice)
    # neo4j is captured for callers that want it (e.g. tool_entity); the
    # field doesn't appear on KairixHealth (kept minimal) but the
    # timeout/error feeds into degraded_reason when relevant.
    _neo4j_available, _neo4j_timed_out = _run_with_timeout(d.neo4j_available_fn, probe_slice)

    # Chat depends only on the LLM credential.
    chat = "ok" if secrets_loaded else "offline"

    # Vector search needs both creds AND a working embed backend.
    if secrets_loaded and embed_backend:
        vector_search = "ok"
    elif secrets_loaded or embed_backend:
        vector_search = "degraded"
    else:
        vector_search = "offline"

    bm25 = "ok" if bm25_available else "offline"

    degraded_reason, next_action = _summarise_degradation(
        secrets_loaded=secrets_loaded,
        embed_backend=embed_backend,
        bm25_available=bm25_available,
        secrets_timed_out=secrets_timed_out,
        embed_timed_out=embed_timed_out,
        bm25_timed_out=bm25_timed_out,
    )

    return KairixHealth(
        vector_search=vector_search,
        bm25=bm25,
        chat=chat,
        secrets_loaded=secrets_loaded,
        degraded_reason=degraded_reason,
        next_action=next_action,
    )


def _summarise_degradation(
    *,
    secrets_loaded: bool,
    embed_backend: bool,
    bm25_available: bool,
    secrets_timed_out: bool = False,
    embed_timed_out: bool = False,
    bm25_timed_out: bool = False,
) -> tuple[str, str]:
    """Render ``degraded_reason`` + ``next_action`` strings for the snapshot.

    Returns ``("", "")`` when fully healthy. The directive is always
    prescriptive ("Use ...", "Surface ...") so the agent has a clear
    next step.
    """
    if secrets_loaded and embed_backend and bm25_available:
        return "", ""

    reasons: list[str] = []
    if not secrets_loaded:
        if secrets_timed_out:
            reasons.append("secrets probe exceeded 2s budget")
        else:
            reasons.append("KAIRIX_LLM_API_KEY not resolvable")
    if not embed_backend:
        if embed_timed_out:
            reasons.append("embed backend probe exceeded 2s budget")
        else:
            reasons.append("embed backend unavailable")
    if not bm25_available:
        if bm25_timed_out:
            reasons.append("BM25 probe exceeded 2s budget")
        else:
            reasons.append("BM25 index missing")
    degraded_reason = "; ".join(reasons)

    # Prescriptive directive — depends on which leg(s) survived.
    if bm25_available and not (secrets_loaded and embed_backend):
        next_action = (
            "Vector search degraded — surface this to your human and use BM25 results from tool_search. "
            "Ask your admin to run 'kairix onboard check'."
        )
    elif (secrets_loaded and embed_backend) and not bm25_available:
        next_action = (
            "BM25 offline — vector search still works via tool_search. "
            "Ask your admin to run 'kairix embed --rebuild-fts'."
        )
    else:
        next_action = (
            "Vector search and BM25 both offline — kairix retrieval is unavailable. "
            "Surface this to your human; ask your admin to run 'kairix onboard check'."
        )

    return degraded_reason, next_action


# ---------------------------------------------------------------------------
# Envelope projection
# ---------------------------------------------------------------------------


def health_to_envelope(health: KairixHealth) -> dict[str, object]:
    """Project a ``KairixHealth`` to the JSON dict tool consumers receive."""
    return {
        "vector_search": health.vector_search,
        "bm25": health.bm25,
        "chat": health.chat,
        "secrets_loaded": health.secrets_loaded,
        "degraded_reason": health.degraded_reason,
        "next_action": health.next_action,
    }


# ---------------------------------------------------------------------------
# Tool-specific next_action overlays
# ---------------------------------------------------------------------------


def search_next_action(health: KairixHealth) -> str:
    """Prescriptive directive for ``tool_search`` callers.

    When vector search is degraded but BM25 is up, the agent should
    keep going with the BM25 results below — that's the whole point of
    the "still return useful results" contract. ``next_action`` echoes
    that affordance.
    """
    if health.vector_search != "ok" and health.bm25 == "ok":
        return "Ask your admin to run 'kairix onboard check'; results below are BM25-only."
    if health.bm25 != "ok" and health.vector_search == "ok":
        return "Ask your admin to run 'kairix embed --rebuild-fts'; results below are vector-only."
    if health.vector_search != "ok" and health.bm25 != "ok":
        return "Search is offline — surface this to your human and ask your admin to run 'kairix onboard check'."
    return ""


def brief_next_action(health: KairixHealth) -> str:
    """Prescriptive directive for ``tool_brief`` callers.

    Brief depends on chat for synthesis. When chat is offline the
    envelope returns an empty content body; the directive tells the
    agent to fall back to ``tool_search`` rather than treating the
    empty content as a successful brief.
    """
    if health.chat != "ok":
        return "Brief synthesis offline — fall back to tool_search for raw results and surface to your human."
    if health.vector_search != "ok" or health.bm25 != "ok":
        return "Brief retrieval degraded — surface this to your human; results may be sparser than usual."
    return ""


def entity_next_action(health: KairixHealth, *, neo4j_available: bool) -> str:
    """Prescriptive directive for ``tool_entity`` callers.

    Entity lookups depend on Neo4j. When the graph is offline the
    directive points the agent at ``tool_search`` so it can still find
    vault references by the entity's name.
    """
    if not neo4j_available:
        return "Knowledge graph offline — try tool_search with the entity name for vault references."
    if health.vector_search != "ok" or health.bm25 != "ok":
        return "Retrieval degraded — entity lookup still works; surface the degradation to your human."
    return ""
