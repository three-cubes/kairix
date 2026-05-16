"""Unit tests for ``tool_probe_burst`` — the operator-only burst escalation stub.

Unlike ``tool_probe_search`` (agent-safe below caps), ``tool_probe_burst`` is
escalation-only: every call — regardless of args — returns the canonical
OperatorOnlyCapability envelope. Burst is load-generating by design, so even
small runs stress the production retrieval pipeline.

These tests pin:
  - The envelope shape (error=OperatorOnlyCapability + required keys).
  - The operator_command names ``kairix probe burst`` AND forwards every arg.
  - Default args match the documented operator-visible defaults.
  - The envelope links to the retrieval runbook so operators have a breadcrumb.
"""

from __future__ import annotations

import inspect

import pytest

from kairix.agents.mcp.server import tool_probe_burst

pytestmark = pytest.mark.unit


# Canonical envelope key used by ``_operator_only_envelope``. Asserting on
# this string is the public contract — agents read ``error`` to decide
# whether they hit an escalation envelope. Lowercased copy is kept here so
# tests don't import the internal helper name (F5).
ESCALATION_ERROR = "OperatorOnlyCapability"


def test_returns_escalation_envelope_with_defaults() -> None:
    """Even with default args, the burst tool returns the escalation envelope.

    Sabotage: change ``tool_probe_burst`` to call ``run_probe_burst`` directly
    and this test fails — agents would silently trigger load generation.
    """
    envelope = tool_probe_burst()

    assert envelope["error"] == ESCALATION_ERROR
    assert envelope["capability"] == "probe burst"
    for key in ("reason", "operator_command", "expected_runtime_seconds", "see_also"):
        assert key in envelope, f"envelope missing {key!r}; got {sorted(envelope.keys())}"


def test_operator_command_forwards_all_args() -> None:
    """The operator_command names ``kairix probe burst`` and includes every CLI flag.

    Sabotage: drop --total-queries or --peak-concurrency from the f-string in
    the stub and an operator copy-pasting the command runs the default
    sized burst, not the agent's requested shape.
    """
    envelope = tool_probe_burst(suite="reflib", total_queries=500, peak_concurrency=50)

    cmd = envelope["operator_command"]
    assert cmd.startswith("kairix probe burst ")
    assert "--suite reflib" in cmd
    assert "--total-queries 500" in cmd
    assert "--peak-concurrency 50" in cmd


def test_runtime_estimate_scales_with_total_queries() -> None:
    """expected_runtime_seconds grows (at least non-decreasing) with total_queries.

    Sabotage: replace the ``max(30, total_queries // 5)`` formula with a
    fixed constant and this assertion catches it — agents would surface a
    misleading wait estimate to their admin.
    """
    small = tool_probe_burst(total_queries=50)
    large = tool_probe_burst(total_queries=1000)
    assert large["expected_runtime_seconds"] >= small["expected_runtime_seconds"]
    assert large["expected_runtime_seconds"] > 30


def test_envelope_links_to_retrieval_runbook() -> None:
    """The envelope must point operators at the retrieval runbook.

    Sabotage: drop the see_also list from the _operator_only_envelope call
    and this assertion fails — agents lose the runbook breadcrumb.
    """
    envelope = tool_probe_burst()

    see_also = envelope["see_also"]
    assert isinstance(see_also, list)
    assert see_also, "see_also must be non-empty"
    assert any("retrieval-health" in entry for entry in see_also), (
        f"expected retrieval-health runbook in see_also, got: {see_also}"
    )


def test_default_args_match_documented_defaults() -> None:
    """Defaults are part of the public contract — pin them.

    Sabotage: change either default in the stub and an agent calling with
    no args would get a different command than the documented baseline.
    """
    sig = inspect.signature(tool_probe_burst)
    assert sig.parameters["suite"].default == "reflib"
    assert sig.parameters["total_queries"].default == 200
    assert sig.parameters["peak_concurrency"].default == 20
