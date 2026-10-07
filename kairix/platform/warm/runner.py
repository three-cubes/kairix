"""Warm-up runner — pay the factory-init + first-search costs at startup.

An agent's first request pays ~192 MB of allocations + factory
wall-time (#279). This runner absorbs that cost so it lands BEFORE
``/healthz/ready`` flips to 200.

Steps:
    1. Build the SearchPipeline (factory: DB connections, Azure embed
       client, BM25 + vector backend init). Costs ~120 MB.
    2. Issue one no-op tool_search (populates per-call caches, builds
       query plan). Costs ~70 MB.
    3. Warm the cross-encoder reranker model when rerank is wired on the
       built pipeline's config (``rerank.enabled`` True OR ``rerank_intents``
       non-empty — the same condition under which the factory wires a real
       reranker closure). The first semantic / multi-hop query would
       otherwise pay a multi-second torch import + model load on the
       request path even though the server already reports ready (PLA-271).
    4. Open the Neo4j client connection (small but waitable).

Never raises — each step is wrapped so a single failure populates a
WarmFailure entry but other steps still attempt to run. Caller decides
whether to flip ``/healthz/ready`` based on ``WarmResult.ok``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# Step names — extracted as constants so the same literal isn't repeated
# across dispatch + skip + post-check sites.
_STEP_BUILD = "build_search_pipeline"
_STEP_PROBE = "probe_search"
_STEP_GRAPH = "open_graph_client"
_STEP_SQLITE_STATS = "ensure_sqlite_stats"
_STEP_RERANK = "warm_cross_encoder"

# Operator-facing per-step detail strings for the cross-encoder warm step.
# Extracted as constants so the skip-reason text lives in one place and the
# step's branches stay readable.
DETAIL_RERANK_NO_PIPELINE = "rerank warm skipped — search pipeline unavailable"
DETAIL_RERANK_NOT_WIRED = "rerank not wired (disabled, no rerank intents) — cross-encoder load skipped"


# Workload signature used for the no-op probe — short, lowercase, ASCII,
# unlikely to match real content in any vault. Tests assert this exact
# string is what the warm-up sends so a regression that changes the probe
# to operator-visible content (e.g. an actual term in a deployed vault)
# is caught.
WARMUP_QUERY = "__kairix_warmup_probe__"


@dataclass(frozen=True)
class WarmStep:
    """Outcome of one warm-up step."""

    name: str
    ok: bool
    duration_s: float
    detail: str = ""


@dataclass(frozen=True)
class WarmFailure:
    """One step that failed."""

    step: str
    detail: str


@dataclass(frozen=True)
class WarmResult:
    """Outcome of one ``run_warm`` invocation.

    Attributes:
        steps: per-step results in execution order.
        failures: structured failures for the non-ok steps.
        ok: True only when every step succeeded.
        total_duration_s: wall time across all steps.
    """

    steps: list[WarmStep] = field(default_factory=list)
    failures: list[WarmFailure] = field(default_factory=list)
    ok: bool = True
    total_duration_s: float = 0.0

    def to_envelope(self) -> dict[str, Any]:
        """Project to the JSON envelope CLI --json + MCP emit."""
        return {
            "ok": self.ok,
            "total_duration_s": self.total_duration_s,
            "steps": [{"name": s.name, "ok": s.ok, "duration_s": s.duration_s, "detail": s.detail} for s in self.steps],
            "failures": [{"step": f.step, "detail": f.detail} for f in self.failures],
        }


def _step_build_pipeline() -> Any:
    """Build the production search pipeline. Pays the ~120 MB factory cost."""
    from kairix.core.factory import build_search_pipeline

    return build_search_pipeline()


def _step_probe_search(pipeline: Any) -> Any:
    """Issue one no-op search through the warmed pipeline.

    Triggers per-call cache population + query-plan compilation that
    would otherwise land on the first agent request. Result is
    discarded — only side-effects matter.
    """
    return pipeline.search(query=WARMUP_QUERY, budget=500, scope="shared+agent")


@dataclass(frozen=True)
class _RerankWarmOutcome:
    """Outcome of the cross-encoder warm step.

    Only ``detail`` is read by the runner (hoisted onto the WarmStep via
    :func:`_with_detail` so the operator-facing envelope explains whether
    the model was warmed or the step was a structural skip).
    """

    detail: str


def _step_warm_cross_encoder(pipeline: Any, loader: Callable[[str], Any]) -> _RerankWarmOutcome:
    """Force-load the cross-encoder reranker model when rerank is wired.

    The reranker is a lazy singleton (:func:`kairix.core.search.rerank.get_cross_encoder`):
    the model isn't loaded until the first rerank-eligible query. That first
    query pays a multi-second torch import + model load on the request path —
    even though ``gate.mark_ready`` already flipped the server to ready. This
    step pays that cost during warm so "ready" genuinely means warm (PLA-271).

    The step only warms when rerank is actually wired on the built pipeline's
    config — ``rerank.enabled`` is True OR ``rerank_intents`` is non-empty.
    That is the exact condition under which the factory
    (``kairix.core.factory._resolve_reranker``) wires a real reranker closure,
    so we never import torch + sentence-transformers for a deployment that has
    rerank fully disabled.

    Args:
        pipeline: the SearchPipeline built by :func:`_step_build_pipeline`.
            Its ``.config`` carries the resolved ``RetrievalConfig`` whose
            ``rerank`` block + ``rerank_intents`` decide whether to warm.
        loader: one-arg callable ``(model_name) -> encoder`` — production
            binds :func:`kairix.core.search.rerank.get_cross_encoder`; tests
            inject a recording fake to prove the load is (not) requested.

    Returns:
        ``_RerankWarmOutcome`` carrying an operator-facing ``detail``.
    """
    cfg = getattr(pipeline, "config", None)
    if cfg is None:
        # No built pipeline (failed build) or a config-less stand-in — there
        # is nothing to warm. The build failure, if any, is reported by the
        # build step; this is a structural skip.
        return _RerankWarmOutcome(DETAIL_RERANK_NO_PIPELINE)
    # cfg is the resolved RetrievalConfig — ``rerank`` (a RerankConfig) and
    # ``rerank_intents`` are always present. ``wired`` mirrors the factory's
    # _resolve_reranker condition exactly: a real reranker closure is wired
    # iff rerank is force-enabled OR at least one intent reranks by default.
    rerank_cfg = cfg.rerank
    wired = bool(rerank_cfg.enabled) or bool(cfg.rerank_intents)
    if not wired:
        return _RerankWarmOutcome(DETAIL_RERANK_NOT_WIRED)
    loader(rerank_cfg.model)
    return _RerankWarmOutcome(f"warmed cross-encoder model {rerank_cfg.model!r}")


def _step_ensure_sqlite_stats() -> Any:
    """Bootstrap SQLite query-planner statistics if missing.

    Idempotent: when ``sqlite_stat1`` is already populated this is a
    structural no-op. On a fresh install with > 0 documents it runs
    ``ANALYZE`` once so the planner picks the right index for hot-path
    queries (avoiding the idx_documents_active vs idx_documents_collection
    regression the 2026-06-02 production audit found).

    Lazy import keeps the warm runner importable on call sites that
    don't have ``sqlite3`` linked at module load.
    """
    import sqlite3

    from kairix.paths import KairixPaths
    from kairix.platform.warm.sqlite_stats import (
        DETAIL_SKIPPED_STATS_PRESENT,
        STEP_NAME,
        WarmStepResult,
        ensure_sqlite_stats,
    )

    paths = KairixPaths.resolve()
    db_path = paths.db_path
    # Fresh-install path: warm can run before any worker has bootstrapped
    # the DB file or its parent directory. Treat "no DB yet" as a skip
    # rather than a hard failure — the planner-stats bootstrap only makes
    # sense once ingest has written rows.
    if not db_path.exists() or not db_path.parent.exists():
        return WarmStepResult(
            name=STEP_NAME,
            ok=True,
            elapsed_ms=0.0,
            detail=DETAIL_SKIPPED_STATS_PRESENT,
        )
    # F77-allow: warm-up runs once per container before the worker loop owns its coordinator
    db = sqlite3.connect(str(db_path))
    try:
        return ensure_sqlite_stats(db, paths)
    finally:
        db.close()


def _step_open_graph_client() -> Any:
    """Open the Neo4j driver connection so the first entity lookup is fast.

    Returns the client whether or not Neo4j is reachable — soft-fail
    semantics so the warm-up doesn't block on an optional subsystem.

    Importing neo4j costs ~22 MB on the Python heap, so when no Neo4j URI
    is configured we skip the load entirely — the entity-graph subsystem
    is auxiliary and gracefully degrades when absent. Operators who have
    Neo4j configured pay the cost (and get a warmed driver); operators
    who don't save the heap.
    """
    from kairix.secrets import neo4j_uri_configured

    if not neo4j_uri_configured():
        return None
    from kairix.knowledge.graph.client import get_client

    client = get_client()
    _ = client.available
    return client


def _time_step(name: str, fn: Callable[[], Any]) -> tuple[WarmStep, Any]:
    """Run one warm-up step under a timer; return the step record + result.

    Catches everything so a single subsystem failure (e.g. Neo4j down)
    doesn't fail the whole warm-up. Errors land in WarmStep.ok=False
    with the exception class + message in ``detail``.
    """
    t_start = time.perf_counter()
    try:
        result = fn()
        duration = round(time.perf_counter() - t_start, 3)
        return WarmStep(name=name, ok=True, duration_s=duration), result
    except Exception as exc:
        duration = round(time.perf_counter() - t_start, 3)
        logger.warning("warm step %s failed: %s", name, exc, exc_info=True)
        return (
            WarmStep(name=name, ok=False, duration_s=duration, detail=f"{type(exc).__name__}: {exc}"),
            None,
        )


def _with_detail(step: WarmStep, result: Any) -> WarmStep:
    """Hoist a step result's ``.detail`` onto the WarmStep for the envelope.

    Steps whose result object carries a human-readable ``.detail`` (the
    sqlite-stats bootstrap and the cross-encoder warm) surface it in the
    per-step envelope record — operators see "ANALYZE complete" / "warmed
    cross-encoder ..." / "rerank not wired ... skipped" without
    cross-referencing a separate field. Steps that return a bare value (or
    ``None``, or an object with no ``detail``) keep their original record.
    """
    detail_attr = getattr(result, "detail", None) if result is not None else None
    if not detail_attr:
        return step
    return WarmStep(name=step.name, ok=step.ok, duration_s=step.duration_s, detail=str(detail_attr))


def _emit_progress(callback: Callable[[str], None] | None, stage_name: str) -> None:
    """Fire ``callback(stage_name)`` if provided, swallowing exceptions.

    Progress reporting is an observability seam — it must never abort
    the warm-up if a downstream callback misbehaves. Tests assert the
    callback is invoked; production wires the WarmProgress holder update.
    """
    if callback is None:
        return
    try:
        callback(stage_name)
    except Exception as exc:
        logger.warning("warm progress callback for stage %s raised: %s", stage_name, exc, exc_info=True)


def _default_load_cross_encoder(model_name: str) -> Any:
    """Production cross-encoder loader — force-loads + caches the singleton.

    Lazy import: keeps the runner importable on call sites that don't link
    sentence-transformers, and the module-level rerank import is cheap
    (torch only loads inside ``get_cross_encoder``, on first call).
    """
    from kairix.core.search.rerank import get_cross_encoder

    return get_cross_encoder(model_name)


@dataclass
class WarmDeps:
    """Injectable step collaborators for :func:`run_warm`.

    Canonical Deps shape (``kairix/worker.py::WorkerDeps``). Production
    callers omit ``deps`` and every field's ``default_factory`` binds the
    real step. ``kairix warm`` (``kairix.platform.warm.cli``) overrides
    ``build_pipeline`` to warm a pipeline built against an operator-supplied
    ``--db-path`` / ``--document-root``; tests construct
    ``WarmDeps(build_pipeline=lambda: fake, ...)`` to avoid spinning up the
    full search pipeline.

    - ``build_pipeline``: zero-arg pipeline builder.
    - ``search_probe``: one-arg ``(pipeline) -> Any`` no-op probe search.
    - ``open_graph_client``: zero-arg Neo4j client opener.
    - ``ensure_sqlite_stats``: zero-arg ANALYZE bootstrap; may return a
      ``WarmStepResult``-shaped object whose ``detail`` is hoisted.
    - ``load_cross_encoder``: one-arg ``(model_name) -> encoder`` loader for
      the rerank warm step (PLA-271). Tests inject a recording fake to prove
      the model load is requested only when rerank is wired on the built
      pipeline's config.
    """

    build_pipeline: Callable[[], Any] = field(default_factory=lambda: _step_build_pipeline)
    search_probe: Callable[[Any], Any] = field(default_factory=lambda: _step_probe_search)
    open_graph_client: Callable[[], Any] = field(default_factory=lambda: _step_open_graph_client)
    ensure_sqlite_stats: Callable[[], Any] = field(default_factory=lambda: _step_ensure_sqlite_stats)
    load_cross_encoder: Callable[[str], Any] = field(default_factory=lambda: _default_load_cross_encoder)


def run_warm(
    *,
    deps: WarmDeps | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> WarmResult:
    """Run all warm-up steps and return a structured result.

    Args:
        deps: :class:`WarmDeps` carrying the per-step collaborators.
            ``None`` (production) binds the real steps.
        progress_callback: optional one-arg callable invoked with the
            stage name each time a step completes (success or failure).
            Default ``None`` — preserves prior behaviour. The CLI wires a
            callback that appends the stage to the live
            :class:`kairix.platform.warm.state.WarmProgress` so the
            ColdStart envelope surfaces real elapsed/remaining time (#390).

    Returns:
        WarmResult. Never raises — top-level errors populate the
        per-step ``detail`` and ``ok=False``.
    """
    from kairix.platform.warm.state import mark_warm, mark_warming

    deps = deps if deps is not None else WarmDeps()
    build = deps.build_pipeline
    probe = deps.search_probe
    open_graph = deps.open_graph_client
    ensure_stats = deps.ensure_sqlite_stats
    load_cross_encoder = deps.load_cross_encoder

    mark_warming()
    t_total_start = time.perf_counter()
    steps: list[WarmStep] = []

    step_build, pipeline = _time_step(_STEP_BUILD, build)
    steps.append(step_build)
    _emit_progress(progress_callback, _STEP_BUILD)

    if pipeline is not None:
        step_probe, _ = _time_step(_STEP_PROBE, lambda: probe(pipeline))
        steps.append(step_probe)
    else:
        steps.append(
            WarmStep(
                name=_STEP_PROBE,
                ok=False,
                duration_s=0.0,
                detail="skipped because build_search_pipeline failed",
            )
        )
    _emit_progress(progress_callback, _STEP_PROBE)

    # Warm the cross-encoder reranker (PLA-271). The step itself reads the
    # built pipeline's config to decide whether rerank is wired; a None
    # pipeline (failed build) surfaces as a structural skip via the
    # _RerankWarmOutcome detail.
    step_rerank, rerank_result = _time_step(
        _STEP_RERANK, lambda: _step_warm_cross_encoder(pipeline, load_cross_encoder)
    )
    steps.append(_with_detail(step_rerank, rerank_result))
    _emit_progress(progress_callback, _STEP_RERANK)

    step_stats, stats_result = _time_step(_STEP_SQLITE_STATS, ensure_stats)
    # When the ensurer returned a WarmStepResult-shaped object, hoist its
    # ``detail`` into the WarmStep so operators see "ANALYZE complete" /
    # "stats already present, skipped" in the envelope without having to
    # cross-reference a separate field.
    steps.append(_with_detail(step_stats, stats_result))
    _emit_progress(progress_callback, _STEP_SQLITE_STATS)

    step_graph, _ = _time_step(_STEP_GRAPH, open_graph)
    steps.append(step_graph)
    _emit_progress(progress_callback, _STEP_GRAPH)

    total_duration = round(time.perf_counter() - t_total_start, 3)
    failures = [WarmFailure(step=s.name, detail=s.detail) for s in steps if not s.ok]

    result = WarmResult(
        steps=steps,
        failures=failures,
        ok=not failures,
        total_duration_s=total_duration,
    )
    # The graph step soft-fails so we accept warm without it — the
    # load-bearing path is search + probe.
    if result.steps[0].ok and any(s.ok for s in result.steps if s.name == _STEP_PROBE):
        mark_warm()
    return result
