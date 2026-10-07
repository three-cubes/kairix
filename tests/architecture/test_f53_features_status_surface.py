"""Unit tests for F53 (``scripts/checks/check_f53_features_status_surface.py``).

F53 enforces that the operator surface for feature flags exists:

  1. ``kairix/cli.py:COMMANDS`` has a ``"features"`` entry.
  2. ``kairix/agents/mcp/server.py`` registers a ``features_status`` MCP
     tool. Post-PLA-318 registration is catalogue-driven: ``build_server``
     registers one tool per ``CAPABILITIES_CATALOG`` row, so the detector
     reads ``registered_mcp_tool_names`` off the ``_cap(...)`` rows — a row
     whose ``mcp_tool`` (agent-callable) OR ``escalate_via`` (operator-stub
     adapter) resolves to ``features_status`` IS the registered tool.
The outcome-test requirement for both surfaces is F30's job (no
grandfather list since PLA-472), so F53 reads no baseline file.

These tests exercise the AST-presence helpers against synthetic source
files. Vacuous-green when ``kairix.core.features`` is not importable.

Each test carries an inline sabotage-proof.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DETECTOR_PATH = _REPO_ROOT / "scripts" / "checks" / "check_f53_features_status_surface.py"


def _load_detector() -> object:
    """Load the F53 detector module by file path."""
    spec = importlib.util.spec_from_file_location("_f53_detector", _DETECTOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_f53_detector"] = module
    spec.loader.exec_module(module)
    return module


def test_commands_has_features_returns_true_when_present(tmp_path: Path) -> None:
    """COMMANDS containing a 'features' key is the positive contract.

    Sabotage proof: remove the key-name check in ``_commands_has_features``
    and this assertion would flip to False.
    """
    detector = _load_detector()
    cli = tmp_path / "cli.py"
    cli.write_text(
        """COMMANDS: dict[str, tuple[str, str, bool]] = {
    "features": ("kairix.core.features.cli", "main", True),
    "search": ("kairix.core.search.cli", "main", True),
}
""",
        encoding="utf-8",
    )
    assert detector._commands_has_features(cli) is True  # type: ignore[attr-defined]  # detector loaded by path; mypy can't see attrs


def test_commands_missing_features_returns_false(tmp_path: Path) -> None:
    """COMMANDS without 'features' is the negative contract.

    Sabotage proof: change the equality from ``"features"`` to ``"any"``
    and this assertion flips.
    """
    detector = _load_detector()
    cli = tmp_path / "cli.py"
    cli.write_text(
        """COMMANDS: dict[str, tuple[str, str, bool]] = {
    "search": ("kairix.core.search.cli", "main", True),
}
""",
        encoding="utf-8",
    )
    assert detector._commands_has_features(cli) is False  # type: ignore[attr-defined]  # detector loaded by path; mypy can't see attrs


def test_mcp_registers_features_status_convention_name_returns_true(tmp_path: Path) -> None:
    """A catalogue ``_cap(mcp_tool="features_status", ...)`` row — the shape
    ``build_server`` walks to register the tool — passes.

    Post-PLA-318 registration is catalogue-driven, so this mirrors the real
    server.py row (``mcp_tool="features_status"``) rather than the retired
    ``@server.tool()`` decorator form.

    Sabotage proof: rename this row's ``mcp_tool`` value to a different tool
    (``"features_status"`` → ``"other_tool"``) and the detector no longer sees
    ``features_status`` registered, flipping this assertion to False.
    """
    detector = _load_detector()
    mcp = tmp_path / "server.py"
    mcp.write_text(
        """CAPABILITIES_CATALOG = (
    _cap(
        name="features_status",
        mcp_tool="features_status",
        cli="kairix features status",
        category=CAP_CATEGORY_DIAGNOSTIC,
    ),
)
""",
        encoding="utf-8",
    )
    assert detector._mcp_registers_features_status(mcp) is True  # type: ignore[attr-defined]  # detector loaded by path; mypy can't see attrs


def test_mcp_registers_features_status_adapter_name_returns_true(tmp_path: Path) -> None:
    """The alternate ``escalate_via="features_status"`` registration also
    passes — a ``_cap`` row registers a tool by its ``mcp_tool``
    (agent-callable) OR its ``escalate_via`` (operator-stub adapter) name,
    and the catalogue reader recognizes both.

    This exercises the ``escalate_via`` branch of ``registered_mcp_tool_names``
    (the real shape used by the operator-only stub rows in server.py).

    Sabotage proof: rename the registration keyword to a non-registration
    argument (``escalate_via`` → ``note``) and the detector stops resolving a
    tool name from this row, flipping this assertion to False.
    """
    detector = _load_detector()
    mcp = tmp_path / "server.py"
    mcp.write_text(
        """CAPABILITIES_CATALOG = (
    _cap(
        name="features_status",
        escalate_via="features_status",
        cli="kairix features status",
        category=CAP_CATEGORY_DIAGNOSTIC,
    ),
)
""",
        encoding="utf-8",
    )
    assert detector._mcp_registers_features_status(mcp) is True  # type: ignore[attr-defined]  # detector loaded by path; mypy can't see attrs


def test_mcp_missing_features_status_returns_false(tmp_path: Path) -> None:
    """A catalogue that registers a different tool does NOT satisfy F53.

    Sabotage proof: change ``_FEATURES_TOOL_NAME`` in the detector to
    ``"search"`` (the tool this catalogue row registers) and this assertion
    flips to True.
    """
    detector = _load_detector()
    mcp = tmp_path / "server.py"
    mcp.write_text(
        """CAPABILITIES_CATALOG = (
    _cap(
        name="search",
        mcp_tool="search",
        cli="kairix search",
        category=CAP_CATEGORY_AGENT,
    ),
)
""",
        encoding="utf-8",
    )
    assert detector._mcp_registers_features_status(mcp) is False  # type: ignore[attr-defined]  # detector loaded by path; mypy can't see attrs


def test_remediation_carries_action_markers() -> None:
    """F53's REMEDIATION must carry F21 ``fix:`` / ``next:`` / ``run:`` markers."""
    detector = _load_detector()
    rem = detector.REMEDIATION.lower()  # type: ignore[attr-defined]  # detector loaded by path; mypy can't see attrs
    assert "fix:" in rem
    assert "next:" in rem
    assert "run:" in rem


def test_vacuous_green_when_module_absent() -> None:
    """The detector returns 0 when kairix.core.features is absent.

    PR-2 may not be landed yet; F53 must not block the gate then.
    """
    detector = _load_detector()
    # In this worktree, kairix.core.features is absent; verifies the
    # short-circuit branch.
    if not detector._features_module_available():  # type: ignore[attr-defined]  # detector loaded by path; mypy can't see attrs
        assert detector.main() == 0  # type: ignore[attr-defined]  # detector loaded by path; mypy can't see attrs
