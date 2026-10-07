"""Unit tests for kairix.platform.warm.run_warm."""

from __future__ import annotations

from typing import Any

import pytest

from kairix.core.search.config import RerankConfig, RetrievalConfig
from kairix.platform.warm import WARMUP_QUERY, WarmDeps, run_warm
from kairix.platform.warm.runner import DETAIL_RERANK_NOT_WIRED
from tests.fakes import FakeCrossEncoderLoader, FakeSearchPipeline

pytestmark = pytest.mark.unit


class _FakePipeline:
    """Stand-in for the SearchPipeline; records every search call."""

    def __init__(self) -> None:
        self.search_calls: list[dict[str, Any]] = []

    def search(self, **kwargs: Any) -> dict[str, Any]:
        self.search_calls.append(kwargs)
        return {"results": []}


class _FakeGraphClient:
    """Stand-in for the Neo4j client."""

    available = True


class _FakeStatsResult:
    """Stand-in for WarmStepResult — only ``.detail`` is read by the runner."""

    def __init__(self, detail: str = "stats already present, skipped") -> None:
        self.detail = detail


def _ok_deps() -> WarmDeps:
    """Builders that all succeed — happy-path injection."""
    fake = _FakePipeline()
    return WarmDeps(
        build_pipeline=lambda: fake,
        search_probe=lambda p: p.search(query="__test__"),
        open_graph_client=_FakeGraphClient,
        ensure_sqlite_stats=lambda: _FakeStatsResult(),
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_runs_every_step() -> None:
    """All five steps execute and report ok=True."""
    result = run_warm(deps=_ok_deps())
    assert result.ok is True
    assert result.failures == []
    assert {s.name for s in result.steps} == {
        "build_search_pipeline",
        "probe_search",
        "warm_cross_encoder",
        "ensure_sqlite_stats",
        "open_graph_client",
    }
    assert all(s.ok for s in result.steps), [(s.name, s.detail) for s in result.steps]
    assert result.total_duration_s >= 0.0


def test_warmup_query_constant_is_log_distinguishable() -> None:
    """The exported WARMUP_QUERY constant is something an operator can
    spot in journald output as 'this was warm-up traffic, not a real
    query'. It must not collide with any plausible vault content.

    Sabotage-proof: drop the prefix/suffix sentinel markers and a
    plausible real query like 'session' would match.
    """
    # Long enough that accidental match is implausible.
    assert len(WARMUP_QUERY) >= 16, f"WARMUP_QUERY too short to be log-distinguishable: {WARMUP_QUERY!r}"
    # Sentinel markers — leading and trailing underscores wrap the string
    # so it never collides with operator-typed content.
    assert WARMUP_QUERY.startswith("__") and WARMUP_QUERY.endswith("__"), (
        f"WARMUP_QUERY should be wrapped in __...__ sentinels: {WARMUP_QUERY!r}"
    )


# ---------------------------------------------------------------------------
# Failure isolation — one bad step doesn't kill the others
# ---------------------------------------------------------------------------


def test_pipeline_build_failure_skips_probe_but_runs_graph() -> None:
    """When build_search_pipeline raises, probe_search is skipped, graph still opens.

    Sabotage: remove the ``if pipeline is not None`` guard and probe_search
    runs against ``None``, masking the upstream failure.
    """

    def boom() -> Any:
        raise RuntimeError("factory exploded")

    result = run_warm(
        deps=WarmDeps(
            build_pipeline=boom,
            search_probe=lambda p: p,
            open_graph_client=_FakeGraphClient,
            ensure_sqlite_stats=lambda: _FakeStatsResult(),
        ),
    )
    assert result.ok is False
    by_name = {s.name: s for s in result.steps}
    assert by_name["build_search_pipeline"].ok is False
    assert "factory exploded" in by_name["build_search_pipeline"].detail
    assert by_name["probe_search"].ok is False
    assert "skipped" in by_name["probe_search"].detail
    # Graph step still attempted — independent subsystem.
    assert by_name["open_graph_client"].ok is True


def test_graph_failure_doesnt_block_search_warmup() -> None:
    """Neo4j down should not fail the whole warm-up.

    The search pipeline is the load-bearing path; graph is auxiliary.
    A warm pipeline + cold graph is far better than failing closed.
    """
    fake = _FakePipeline()

    def boom_graph() -> Any:
        raise ConnectionError("Neo4j unreachable")

    result = run_warm(
        deps=WarmDeps(
            build_pipeline=lambda: fake,
            search_probe=lambda p: p.search(query="__test__"),
            open_graph_client=boom_graph,
            ensure_sqlite_stats=lambda: _FakeStatsResult(),
        ),
    )
    assert result.ok is False
    by_name = {s.name: s for s in result.steps}
    assert by_name["build_search_pipeline"].ok is True
    assert by_name["probe_search"].ok is True
    assert by_name["open_graph_client"].ok is False
    assert "Neo4j unreachable" in by_name["open_graph_client"].detail


# ---------------------------------------------------------------------------
# Envelope shape — pinned by the design doc
# ---------------------------------------------------------------------------


def test_envelope_shape_matches_design_spec() -> None:
    env = run_warm(deps=_ok_deps()).to_envelope()
    for key in ("ok", "total_duration_s", "steps", "failures"):
        assert key in env, f"missing key {key!r}; got {sorted(env.keys())}"
    for step in env["steps"]:
        for key in ("name", "ok", "duration_s", "detail"):
            assert key in step, f"step missing {key!r}; got {sorted(step.keys())}"


# ---------------------------------------------------------------------------
# Progress callback — #390 wiring for live ColdStart envelope remaining
# ---------------------------------------------------------------------------


def test_progress_callback_fires_once_per_stage_in_order() -> None:
    """When ``progress_callback`` is supplied, each stage completion fires it.

    Pins the #390 wiring: the CLI registers a WarmProgress holder at
    warm-start and the callback appends each completed stage so the live
    ColdStart envelope can surface elapsed/remaining time. Sabotage-proof:
    remove an ``_emit_progress`` call after a step and the stage is missing
    from ``observed``.
    """
    observed: list[str] = []

    run_warm(progress_callback=observed.append, deps=_ok_deps())

    assert observed == [
        "build_search_pipeline",
        "probe_search",
        "warm_cross_encoder",
        "ensure_sqlite_stats",
        "open_graph_client",
    ], f"progress callback must fire once per stage in execution order; got {observed!r}"


def test_progress_callback_exception_does_not_abort_warm() -> None:
    """A misbehaving callback must not break warm-up.

    Observability seams are best-effort — if the CLI's WarmProgress
    update raises (e.g. holder mutation race), warm must still complete.
    Sabotage-proof: drop the ``try/except`` in ``_emit_progress`` and the
    second stage never runs because the first stage's callback raises.
    """

    def explode(_stage_name: str) -> None:
        raise RuntimeError("observer boom")

    result = run_warm(progress_callback=explode, deps=_ok_deps())

    assert result.ok is True, f"callback exception must not abort warm; got {result.failures!r}"
    assert {s.name for s in result.steps} == {
        "build_search_pipeline",
        "probe_search",
        "warm_cross_encoder",
        "ensure_sqlite_stats",
        "open_graph_client",
    }


# ---------------------------------------------------------------------------
# Cross-encoder warm step — PLA-271. "ready" must mean "rerank model loaded".
# ---------------------------------------------------------------------------


_TEST_RERANK_MODEL = "cross-encoder/test-warm-model"


def _rerank_deps(cfg: RetrievalConfig, loader: FakeCrossEncoderLoader) -> WarmDeps:
    """Happy-path deps whose built pipeline carries ``cfg`` and whose
    cross-encoder load is routed through the recording ``loader``."""
    pipeline = FakeSearchPipeline(config=cfg)
    return WarmDeps(
        build_pipeline=lambda: pipeline,
        search_probe=lambda p: p.search(query="__test__"),
        open_graph_client=_FakeGraphClient,
        ensure_sqlite_stats=lambda: _FakeStatsResult(),
        load_cross_encoder=loader,
    )


def _rerank_step(result: Any) -> Any:
    """Return the single ``warm_cross_encoder`` step record from a WarmResult."""
    matches = [s for s in result.steps if s.name == "warm_cross_encoder"]
    assert len(matches) == 1, f"expected exactly one warm_cross_encoder step; got {[s.name for s in result.steps]}"
    return matches[0]


def test_warms_cross_encoder_when_rerank_wired_via_intents() -> None:
    """When ``rerank_intents`` is non-empty (the default), warm force-loads
    the cross-encoder so the first semantic / multi-hop query doesn't pay the
    torch import + model load on the request path (PLA-271).

    Sabotage-proof (executed): deleting the ``loader(model)`` call in
    ``_step_warm_cross_encoder`` drops ``loader.models`` to ``[]`` and this
    assertion fails.
    """
    # Default RetrievalConfig: rerank.enabled=False but rerank_intents are
    # ("multi_hop", "semantic") — so the factory wires a real reranker and the
    # model WILL load on the first matching query.
    cfg = RetrievalConfig(rerank=RerankConfig(model=_TEST_RERANK_MODEL))
    assert cfg.rerank.enabled is False and cfg.rerank_intents, "fixture must be wired via intents, not enabled"
    loader = FakeCrossEncoderLoader()

    result = run_warm(deps=_rerank_deps(cfg, loader))

    assert loader.models == [_TEST_RERANK_MODEL], (
        f"warm must force-load the config's rerank model exactly once; got {loader.models!r}"
    )
    assert _rerank_step(result).ok is True


def test_warms_cross_encoder_when_rerank_force_enabled() -> None:
    """``rerank.enabled=True`` with no rerank_intents is still wired (operator
    force-enable), so warm loads the model.

    Sabotage-proof (executed): narrowing the guard to ``rerank_intents`` only
    (dropping the ``rerank.enabled`` limb) leaves ``loader.models`` empty here.
    """
    cfg = RetrievalConfig(rerank=RerankConfig(enabled=True, model=_TEST_RERANK_MODEL), rerank_intents=())
    loader = FakeCrossEncoderLoader()

    result = run_warm(deps=_rerank_deps(cfg, loader))

    assert loader.models == [_TEST_RERANK_MODEL], f"force-enabled rerank must warm the model; got {loader.models!r}"
    assert _rerank_step(result).ok is True


def test_does_not_warm_cross_encoder_when_rerank_fully_disabled() -> None:
    """When rerank is fully disabled (``enabled=False`` AND no rerank_intents)
    the factory wires no reranker, so warm must NOT import torch / load a model.

    Sabotage-proof (executed): removing the ``wired`` guard in
    ``_step_warm_cross_encoder`` makes the loader fire unconditionally —
    ``loader.calls`` becomes 1 and this assertion fails.
    """
    cfg = RetrievalConfig(rerank=RerankConfig(enabled=False, model=_TEST_RERANK_MODEL), rerank_intents=())
    loader = FakeCrossEncoderLoader()

    result = run_warm(deps=_rerank_deps(cfg, loader))

    assert loader.calls == 0, f"disabled rerank must not load the cross-encoder; got models={loader.models!r}"
    step = _rerank_step(result)
    assert step.ok is True, f"the skip is a structural no-op, not a failure; got detail={step.detail!r}"
    assert step.detail == DETAIL_RERANK_NOT_WIRED, f"skip detail should explain the no-op; got {step.detail!r}"
