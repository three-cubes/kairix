"""Warm-up runner — pay the factory-init + first-search costs at startup.

Live profile from the v2026.5.16a3 alpha (#279) showed an agent's first
request paid ~192 MB of allocations + factory wall-time. This runner
absorbs that cost so it lands BEFORE ``/healthz/ready`` flips to 200.

Steps:
    1. Build the SearchPipeline (factory: DB connections, Azure embed
       client, BM25 + vector backend init). Costs ~120 MB.
    2. Issue one no-op tool_search (populates per-call caches, builds
       query plan). Costs ~70 MB.
    3. Open the Neo4j client connection (small but waitable).

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


def _step_open_graph_client() -> Any:
    """Open the Neo4j driver connection so the first entity lookup is fast.

    Returns the client whether or not Neo4j is reachable — soft-fail
    semantics so the warm-up doesn't block on an optional subsystem.
    """
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


def run_warm(
    *,
    pipeline_builder: Callable[[], Any] | None = None,
    search_probe: Callable[[Any], Any] | None = None,
    graph_client_opener: Callable[[], Any] | None = None,
) -> WarmResult:
    """Run all warm-up steps and return a structured result.

    Args:
        pipeline_builder: injectable; tests pass a fake to avoid spinning
            up the full search pipeline. Production omits.
        search_probe: injectable; tests pass a no-op that accepts the
            pipeline argument and returns immediately.
        graph_client_opener: injectable; tests pass a fake.

    Returns:
        WarmResult. Never raises — top-level errors populate the
        per-step ``detail`` and ``ok=False``.
    """
    from kairix.platform.warm.state import mark_warm, mark_warming

    build = pipeline_builder or _step_build_pipeline
    probe = search_probe or _step_probe_search
    open_graph = graph_client_opener or _step_open_graph_client

    mark_warming()
    t_total_start = time.perf_counter()
    steps: list[WarmStep] = []

    step_build, pipeline = _time_step(_STEP_BUILD, build)
    steps.append(step_build)

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

    step_graph, _ = _time_step(_STEP_GRAPH, open_graph)
    steps.append(step_graph)

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
