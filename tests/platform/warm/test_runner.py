"""Unit tests for kairix.platform.warm.run_warm."""

from __future__ import annotations

from typing import Any

import pytest

from kairix.platform.warm import WARMUP_QUERY, run_warm

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


def _ok_deps() -> dict[str, Any]:
    """Builders that all succeed — happy-path injection."""
    fake = _FakePipeline()
    return {
        "pipeline_builder": lambda: fake,
        "search_probe": lambda p: p.search(query="__test__"),
        "graph_client_opener": _FakeGraphClient,
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_runs_every_step() -> None:
    """All three steps execute and report ok=True."""
    result = run_warm(**_ok_deps())
    assert result.ok is True
    assert result.failures == []
    assert {s.name for s in result.steps} == {"build_search_pipeline", "probe_search", "open_graph_client"}
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
        pipeline_builder=boom,
        search_probe=lambda p: p,
        graph_client_opener=_FakeGraphClient,
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
        pipeline_builder=lambda: fake,
        search_probe=lambda p: p.search(query="__test__"),
        graph_client_opener=boom_graph,
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
    env = run_warm(**_ok_deps()).to_envelope()
    for key in ("ok", "total_duration_s", "steps", "failures"):
        assert key in env, f"missing key {key!r}; got {sorted(env.keys())}"
    for step in env["steps"]:
        for key in ("name", "ok", "duration_s", "detail"):
            assert key in step, f"step missing {key!r}; got {sorted(step.keys())}"
