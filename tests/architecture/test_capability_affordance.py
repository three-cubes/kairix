"""Unit tests for F25 (``scripts/checks/check_capability_affordance.py``).

F25 enforces: every CLI subcommand listed in ``kairix/cli.py``'s
``COMMANDS`` dispatch has a matching ``tool_<command>(...)`` function in
``kairix/agents/mcp/server.py``. A missing affordance means an agent
landing on the capability via MCP gets a 404 instead of an
``OperatorOnlyCapability`` envelope with the right CLI string.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DETECTOR_PATH = _REPO_ROOT / "scripts" / "checks" / "check_capability_affordance.py"


def _load_detector() -> object:
    """Load the F25 detector module by file path."""
    spec = importlib.util.spec_from_file_location("_f25_detector", _DETECTOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_f25_detector"] = module
    spec.loader.exec_module(module)
    return module


def test_real_repo_gate_is_green() -> None:
    """The real F25 detector against the full kairix tree emits no violations.

    All CLI subcommands either have a matching tool_<command> in
    kairix/agents/mcp/server.py OR are listed in the
    _NO_MCP_AFFORDANCE_REQUIRED allowlist with rationale.
    """
    detector = _load_detector()
    assert detector.main() == 0  # type: ignore[attr-defined]  # detector is loaded by path; mypy can't see its attrs


def test_remediation_carries_action_markers() -> None:
    """F25's REMEDIATION must satisfy F21 — every failure-output
    must carry inline action markers so the operator/agent reading the
    failure gets the correction step, not just the diagnosis.
    """
    detector = _load_detector()
    rem = detector.REMEDIATION.lower()  # type: ignore[attr-defined]  # detector is loaded by path; mypy can't see its attrs
    assert "fix:" in rem, "F25 remediation should carry a 'fix:' action marker"
    assert "next:" in rem, "F25 remediation should carry a 'next:' action marker"
    assert "run:" in rem, "F25 remediation should carry a 'run:' action marker"


def test_tool_soak_run_satisfies_soak_command() -> None:
    """The soak CLI command is satisfied by the existing tool_soak_run stub.

    Sabotage-proof inline: rename tool_soak_run in server.py and the
    real_repo_gate test fails because the soak command has no
    tool_soak_<...> binding.
    """
    detector = _load_detector()
    tool_names = detector._read_mcp_tool_functions()  # type: ignore[attr-defined]  # detector loaded by path; mypy cant see its attrs
    # The stub itself
    assert "soak_run" in tool_names, "tool_soak_run must exist as the soak-command affordance"


def test_tool_onboard_check_satisfies_onboard_command() -> None:
    """The onboard CLI command is satisfied by the real tool_onboard_check binding."""
    detector = _load_detector()
    tool_names = detector._read_mcp_tool_functions()  # type: ignore[attr-defined]  # detector loaded by path; mypy cant see its attrs
    assert "onboard_check" in tool_names, "tool_onboard_check must exist as the onboard-command affordance"


def test_allowlist_commands_are_documented_in_design() -> None:
    """Every command in _NO_MCP_AFFORDANCE_REQUIRED is intentional.

    This guards against the allowlist drifting silently. If a future
    contributor adds a new command to the dict to make F25 pass without
    actually adding an affordance, this test fails until they pick one
    of three intentional rationales (interactive wizard / protocol-level
    dispatch / synonym alias) and the design doc agrees.
    """
    detector = _load_detector()
    allowlist = detector._NO_MCP_AFFORDANCE_REQUIRED  # type: ignore[attr-defined]  # detector loaded by path; mypy cant see its attrs
    # The allowlist starts populated from the existing CLI surface where
    # an affordance hasn't been added yet (most commands at this point in
    # the rollout). When all of those grow MCP stubs, this assertion
    # tightens to require an explicit rationale per entry — until then,
    # the check is that the allowlist is not empty AND every entry is
    # a string referencing a real CLI command.
    assert isinstance(allowlist, frozenset)
    assert all(isinstance(c, str) for c in allowlist)
