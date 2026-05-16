"""Tests for the operational MCP tools — diagnostic + operator-only escalations.

Covers:
- tool_onboard_check / tool_worker_status: read-only health probes that
  return the same envelope as their CLI counterparts.
- tool_soak_run / tool_benchmark_run / tool_embed / tool_store_crawl /
  tool_embed_rebuild_fts: operator-only stubs that return the canonical
  OperatorOnlyCapability envelope so agents can escalate cleanly.
"""

from __future__ import annotations

import pytest

from kairix.agents.mcp.server import (
    tool_benchmark_run,
    tool_embed,
    tool_embed_rebuild_fts,
    tool_onboard_check,
    tool_probe_burst,
    tool_soak_run,
    tool_store_crawl,
    tool_worker_status,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Diagnostic tools — read-only state envelopes
# ---------------------------------------------------------------------------


def test_onboard_check_returns_structured_envelope() -> None:
    """The MCP tool returns the same envelope shape as `kairix onboard check --json`."""
    env = tool_onboard_check()
    for key in ("passed", "total", "fully_passed", "failures", "error"):
        assert key in env, f"envelope missing {key!r}; got {sorted(env.keys())}"
    assert isinstance(env["passed"], int)
    assert isinstance(env["total"], int)
    assert isinstance(env["fully_passed"], bool)
    assert isinstance(env["failures"], list)


def test_worker_status_returns_available_or_error_envelope() -> None:
    """The MCP tool returns either an `available: True` envelope or a clean error."""
    env = tool_worker_status()
    assert "available" in env
    assert "error" in env
    if not env["available"]:
        assert env["error"], "unavailable status must carry a populated error"


# ---------------------------------------------------------------------------
# Operator-only stubs — every one returns the canonical envelope shape
# ---------------------------------------------------------------------------

# (tool, kwargs, expected_capability_field, expected_command_substring)
_STUB_CASES = [
    (tool_soak_run, {"suite": "reflib", "repeat": 3}, "soak run", "kairix soak run --suite reflib --repeat 3"),
    (tool_benchmark_run, {"suite": "reflib"}, "benchmark run", "kairix benchmark run --suite reflib"),
    (tool_embed, {"limit": 0}, "embed", "kairix embed"),
    (tool_embed, {"limit": 100}, "embed", "kairix embed --limit 100"),
    (tool_store_crawl, {}, "store crawl", "kairix store crawl"),
    (tool_embed_rebuild_fts, {}, "embed rebuild-fts", "kairix embed rebuild-fts"),
    (
        tool_probe_burst,
        {"suite": "reflib", "total_queries": 200, "peak_concurrency": 20},
        "probe burst",
        "kairix probe burst --suite reflib --total-queries 200 --peak-concurrency 20",
    ),
]


@pytest.mark.parametrize(
    ("tool", "kwargs", "expected_capability", "expected_command"),
    _STUB_CASES,
    ids=[c[2] for c in _STUB_CASES],
)
def test_operator_only_stubs_return_canonical_envelope(
    tool: object, kwargs: dict, expected_capability: str, expected_command: str
) -> None:
    """Each operator-only stub returns the OperatorOnlyCapability envelope.

    Sabotage-proof: change a stub to return None or omit the
    `operator_command` field and this parametric test fails immediately.
    """
    env = tool(**kwargs)  # type: ignore[operator]  # parametric sweep over stubs with mixed kwargs — each row pairs callable + kwargs correctly

    for key in ("error", "capability", "reason", "operator_command", "expected_runtime_seconds", "see_also"):
        assert key in env, f"{expected_capability!r} envelope missing {key!r}; got {sorted(env.keys())}"

    assert env["error"] == "OperatorOnlyCapability", (
        f"{expected_capability!r} should error=OperatorOnlyCapability; got {env['error']!r}"
    )
    assert env["capability"] == expected_capability
    assert env["operator_command"] == expected_command, (
        f"{expected_capability!r} should give the exact CLI command; got {env['operator_command']!r}"
    )
    assert isinstance(env["expected_runtime_seconds"], int)
    assert env["expected_runtime_seconds"] > 0
    assert isinstance(env["see_also"], list)


def test_soak_run_runtime_scales_with_repeat() -> None:
    """The expected_runtime_seconds field reflects the operator-supplied repeat count.

    An agent surfacing this to its admin needs an accurate estimate to set
    a reasonable wait — a fixed-runtime stub would mislead.
    """
    short = tool_soak_run(suite="reflib", repeat=2)
    long_ = tool_soak_run(suite="reflib", repeat=10)
    assert long_["expected_runtime_seconds"] > short["expected_runtime_seconds"]
