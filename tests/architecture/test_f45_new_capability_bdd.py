"""Unit tests for F45 (``scripts/checks/check_f45_new_capability_bdd.py``).

F45 enforces that any commit adding a new top-level capability — CLI
subcommand, MCP tool, or plugin factory — also adds a matching
``tests/bdd/features/*.feature`` in the same commit.

These tests exercise the public ``collect_violations`` entry point
directly with synthetic surface lists and a synthetic staged path
set, so the rule is provable without a real git index.

Each test carries an inline sabotage-proof.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DETECTOR_PATH = _REPO_ROOT / "scripts" / "checks" / "check_f45_new_capability_bdd.py"


def _load_detector():
    """Load the F45 detector module by file path."""
    spec = importlib.util.spec_from_file_location("_f45_detector", _DETECTOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_f45_detector"] = module
    spec.loader.exec_module(module)
    return module


def _write_cli_with_command(workdir: Path, command_name: str) -> Path:
    """Write a minimal kairix/cli.py declaring a single COMMANDS row."""
    cli = workdir / "kairix" / "cli.py"
    cli.parent.mkdir(parents=True, exist_ok=True)
    cli.write_text(
        f'''"""Synthetic CLI for F45 test."""

COMMANDS: dict[str, tuple[str, str, bool]] = {{
    "{command_name}": ("kairix.{command_name.replace("-", "_")}", "main", True),
}}
''',
        encoding="utf-8",
    )
    return cli


def _write_feature(workdir: Path, name: str) -> Path:
    """Write a minimal tests/bdd/features/<name>.feature file."""
    feature = workdir / "tests" / "bdd" / "features" / f"{name}.feature"
    feature.parent.mkdir(parents=True, exist_ok=True)
    feature.write_text(
        f"Feature: {name}\n"
        f"  Scenario: happy path\n"
        f"    Given a kairix process configured with FakePaths\n"
        f"    When the operator runs the new capability\n"
        f"    Then the expected envelope is printed\n",
        encoding="utf-8",
    )
    return feature


def test_new_cli_subcommand_without_feature_is_flagged(tmp_path: Path) -> None:
    """A staged-mock layout containing a new COMMANDS row but no
    matching feature file MUST be flagged.

    Sabotage-proof inline: add the matching feature file to the staged
    set; the violation clears.
    """
    detector = _load_detector()
    _write_cli_with_command(tmp_path, "new-cmd")

    # Staged set: only the cli.py change, no feature file.
    staged: set[Path] = {Path("kairix/cli.py")}
    new_surfaces: list[tuple[str, str, str | None]] = [("cli", "new-cmd", None)]

    pairs = detector.collect_violations(new_surfaces, staged, tree_root=None)
    surfaces = {label for label, _ in pairs}
    assert "cli:new-cmd" in surfaces, "F45 should flag a new CLI subcommand without a feature file"
    # Suggested feature path uses underscore for hyphens.
    suggested = {p for _, p in pairs}
    assert Path("tests/bdd/features/cli_new_cmd.feature") in suggested

    # Sabotage: stage the matching feature.
    _write_feature(tmp_path, "cli_new_cmd")
    staged_with_feature = staged | {Path("tests/bdd/features/cli_new_cmd.feature")}
    pairs_after = detector.collect_violations(new_surfaces, staged_with_feature, tree_root=None)
    assert pairs_after == [], "F45 should clear once the matching feature is staged"


def test_new_cli_subcommand_with_feature_passes(tmp_path: Path) -> None:
    """The companion case: the new COMMANDS row AND the matching
    feature file land together — no violation.

    Sabotage-proof inline: drop the feature file from the staged
    set; the violation re-appears.
    """
    detector = _load_detector()
    _write_cli_with_command(tmp_path, "new-cmd")
    _write_feature(tmp_path, "cli_new_cmd")

    staged: set[Path] = {
        Path("kairix/cli.py"),
        Path("tests/bdd/features/cli_new_cmd.feature"),
    }
    new_surfaces: list[tuple[str, str, str | None]] = [("cli", "new-cmd", None)]
    assert detector.collect_violations(new_surfaces, staged, tree_root=None) == []

    # Sabotage: drop the feature from the staged set.
    staged_without = {Path("kairix/cli.py")}
    pairs = detector.collect_violations(new_surfaces, staged_without, tree_root=None)
    surfaces = {label for label, _ in pairs}
    assert "cli:new-cmd" in surfaces


def test_real_repo_gate_is_green() -> None:
    """The real F45 detector run against the full repo emits no
    violations (there is no grandfathering).

    F45 in default (staged) mode against a clean tree finds nothing,
    so the gate stays green. This guards against regressions in the
    git-diff plumbing — if a future refactor crashes the staged scan,
    this test catches it.
    """
    detector = _load_detector()
    assert detector.main([]) == 0


def test_remediation_carries_action_markers() -> None:
    """F45's REMEDIATION must satisfy F21 — action markers present."""
    detector = _load_detector()
    rem = detector.REMEDIATION.lower()
    assert "fix:" in rem
    assert "next:" in rem
    assert "run:" in rem


def test_override_pointer_satisfies_rule(tmp_path: Path) -> None:
    """The ``# F45-feature: <path>`` override comment lets a surface
    point at a non-conventionally-named feature file.

    Sabotage-proof inline: drop the pointed-at feature from the
    staged set; the violation re-appears (because the convention
    file is also absent).
    """
    detector = _load_detector()
    custom_path = "tests/bdd/features/my_custom_named.feature"
    new_surfaces: list[tuple[str, str, str | None]] = [("cli", "new-cmd", custom_path)]
    staged: set[Path] = {
        Path("kairix/cli.py"),
        Path(custom_path),
    }
    assert detector.collect_violations(new_surfaces, staged, tree_root=None) == []

    # Sabotage: remove the pointed-at file from the staged set.
    staged_broken = {Path("kairix/cli.py")}
    pairs = detector.collect_violations(new_surfaces, staged_broken, tree_root=None)
    surfaces = {label for label, _ in pairs}
    assert "cli:new-cmd" in surfaces
