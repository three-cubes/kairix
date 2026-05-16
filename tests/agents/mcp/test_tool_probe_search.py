"""Unit tests for ``tool_probe_search`` — the agent-safe capped probe surface.

Unlike the other quality tools (soak, benchmark) which are escalation-only,
``tool_probe_search`` is the one quality capability an agent can invoke
itself — but ONLY below the documented caps. These tests pin both halves
of the contract:

  - Below the cap, the call delegates to ``run_probe_search`` and returns
    the ProbeResult envelope.
  - Above the cap (either dimension), it returns the
    OperatorOnlyCapability envelope with the exact CLI command an
    operator should run instead.

Test seam: ``run_probe_search`` is patched on its source module
(``kairix.quality.probe``) via ``monkeypatch.setattr``. The local
``from kairix.quality.probe import run_probe_search`` inside
``tool_probe_search`` resolves to the swapped attribute at call time,
so the swap intercepts the call without modifying production code
(same pattern as ``tests/test_top_level_cli_dispatch.py``).
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any

import pytest

import kairix.quality.probe as probe_module
from kairix.agents.mcp.server import (
    MCP_PROBE_CONCURRENCY_CAP,
    MCP_PROBE_QUERIES_CAP,
    tool_probe_search,
)

pytestmark = pytest.mark.unit


# Canonical envelope key used by ``_operator_only_envelope``. Asserting on
# this string is the public contract — agents read ``error`` to decide
# whether they hit an escalation envelope. Lowercased copy is kept here so
# tests don't import the internal helper name (F5).
ESCALATION_ERROR = "OperatorOnlyCapability"


@dataclass
class _StubProbeResult:
    """Fake ProbeResult — exposes ``to_envelope`` matching the production shape."""

    suite: str
    queries: int
    concurrency: int
    seed: int
    passed: bool = True

    def to_envelope(self) -> dict[str, Any]:
        return {
            "suite": self.suite,
            "queries": self.queries,
            "concurrency": self.concurrency,
            "seed": self.seed,
            "overall": {"p95_ms": 123.0, "n": self.queries},
            "passed": self.passed,
        }


@pytest.fixture
def captured_probe_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Swap ``run_probe_search`` on its source module and capture every call.

    Returns a list that tests append-read to verify forwarding. Swapping the
    module attribute intercepts the local ``from kairix.quality.probe import
    run_probe_search`` inside ``tool_probe_search``.
    """
    captured: list[dict[str, Any]] = []

    def fake_run_probe_search(**kwargs: Any) -> _StubProbeResult:
        captured.append(kwargs)
        return _StubProbeResult(
            suite=kwargs["suite"],
            queries=kwargs["queries"],
            concurrency=kwargs["concurrency"],
            seed=kwargs["seed"],
        )

    monkeypatch.setattr(probe_module, "run_probe_search", fake_run_probe_search)
    return captured


def test_under_cap_runs_probe_and_returns_envelope(
    captured_probe_calls: list[dict[str, Any]],
) -> None:
    """queries=10, concurrency=2 is under both caps → probe actually runs."""
    # Sabotage: flip the cap check from > to >= and this test fails (10 wouldn't run).
    envelope = tool_probe_search(suite="reflib", queries=10, concurrency=2, seed=7)

    # Probe was invoked once with the forwarded arguments.
    assert captured_probe_calls == [{"suite": "reflib", "queries": 10, "concurrency": 2, "seed": 7}]

    # Returned envelope is the ProbeResult shape, NOT the escalation shape.
    assert "error" not in envelope
    assert envelope["suite"] == "reflib"
    assert envelope["queries"] == 10
    assert envelope["concurrency"] == 2
    assert envelope["passed"] is True
    assert "overall" in envelope


def test_at_cap_still_runs(captured_probe_calls: list[dict[str, Any]]) -> None:
    """queries=20 and concurrency=3 are at the cap, not above it → still runs."""
    # Sabotage: change the cap-check operators from > to >= and at-cap calls
    # would escalate instead of running, failing this assertion.
    envelope = tool_probe_search(queries=MCP_PROBE_QUERIES_CAP, concurrency=MCP_PROBE_CONCURRENCY_CAP)

    assert "error" not in envelope
    assert len(captured_probe_calls) == 1
    assert captured_probe_calls[0]["queries"] == MCP_PROBE_QUERIES_CAP
    assert captured_probe_calls[0]["concurrency"] == MCP_PROBE_CONCURRENCY_CAP


def test_queries_over_cap_escalates(captured_probe_calls: list[dict[str, Any]]) -> None:
    """queries=21 → escalation envelope, probe NOT invoked."""
    # Sabotage: remove the queries half of the cap check and this returns
    # a probe envelope instead of the escalation envelope.
    envelope = tool_probe_search(queries=21, concurrency=1)

    assert envelope["error"] == ESCALATION_ERROR
    assert envelope["capability"] == "probe search (above cap)"
    assert "kairix probe search" in envelope["operator_command"]
    assert "--queries 21" in envelope["operator_command"]
    # Probe must not have been invoked when escalating.
    assert captured_probe_calls == []


def test_concurrency_over_cap_escalates(captured_probe_calls: list[dict[str, Any]]) -> None:
    """concurrency=4 → escalation envelope, probe NOT invoked."""
    # Sabotage: remove the concurrency half of the cap check and this returns
    # a probe envelope instead of escalating.
    envelope = tool_probe_search(queries=5, concurrency=4)

    assert envelope["error"] == ESCALATION_ERROR
    assert "--concurrency 4" in envelope["operator_command"]
    assert captured_probe_calls == []


def test_both_dimensions_over_cap_returns_single_envelope(
    captured_probe_calls: list[dict[str, Any]],
) -> None:
    """queries=50, concurrency=10 → ONE escalation envelope with both flags."""
    # Sabotage: replace the ``or`` in the cap check with ``and`` and a
    # queries-only escalation request would not escalate (since concurrency
    # would still satisfy the and-clause depending on operand order). The
    # combined test still escalates here, but checking both flags appear
    # documents the contract that the operator gets the full reproducer.
    envelope = tool_probe_search(suite="reflib", queries=50, concurrency=10, seed=99)

    assert envelope["error"] == ESCALATION_ERROR
    cmd = envelope["operator_command"]
    assert "--queries 50" in cmd
    assert "--concurrency 10" in cmd
    assert "--suite reflib" in cmd
    assert "--seed 99" in cmd
    assert captured_probe_calls == []


def test_default_args_match_documented_caps() -> None:
    """Defaults must equal the caps — the agent-safe surface is opt-out-only."""
    # Sabotage: change either default to a smaller number and an agent calling
    # with no args would get a smaller probe than the cap permits.
    sig = inspect.signature(tool_probe_search)
    assert sig.parameters["suite"].default == "reflib"
    assert sig.parameters["queries"].default == 20
    assert sig.parameters["concurrency"].default == 3
    assert sig.parameters["seed"].default == 0


def test_cap_constants_have_documented_values() -> None:
    """The cap constants are the project's published contract — pin them."""
    # Sabotage: bump either cap upward in server.py and this test fails,
    # forcing the change to be intentional + visible in the diff.
    assert MCP_PROBE_QUERIES_CAP == 20
    assert MCP_PROBE_CONCURRENCY_CAP == 3


def test_escalation_envelope_links_to_retrieval_runbook() -> None:
    """The escalation envelope must point operators at the retrieval runbook."""
    # Sabotage: drop the see_also list from _operator_only_envelope call and
    # this assertion fails — agents lose the runbook breadcrumb.
    envelope = tool_probe_search(queries=100)

    assert envelope["error"] == ESCALATION_ERROR
    see_also = envelope["see_also"]
    assert isinstance(see_also, list)
    assert see_also, "see_also must be a non-empty list"
    assert any("retrieval-health" in entry for entry in see_also), (
        f"expected retrieval-health runbook in see_also, got: {see_also}"
    )
