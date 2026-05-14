"""Unit tests for F21 (``scripts/checks/check_actionable_feedback.py``).

F21 requires every check-script remediation/error string to contain at
least one of the three lowercase action markers — ``fix:``, ``next:``,
or ``run:``. These tests exercise three slices:

1. A synthetic Python check whose REMEDIATION carries ``fix:`` passes.
2. A synthetic Python check whose REMEDIATION carries no marker fails.
3. The real ``scripts/checks/check_*.py`` files in the repo emit no
   *net-new* violations (existing offenders are baselined; this is the
   gate-in-action test).

Each test has a paired sabotage-proof, either inline or as a derived
"flip a marker, confirm it fires" assertion.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DETECTOR_PATH = _REPO_ROOT / "scripts" / "checks" / "check_actionable_feedback.py"


def _load_detector():
    """Load the F21 detector module by file path (it lives outside the
    kairix package so we can't ``import`` it normally).
    """
    spec = importlib.util.spec_from_file_location("_f21_detector", _DETECTOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_f21_detector"] = module
    spec.loader.exec_module(module)
    return module


def test_passing_check_has_action_markers(tmp_path: Path) -> None:
    """A synthetic check_*.py whose REMEDIATION carries ``fix:`` is clean.

    Sabotage-proof: the same module without ``fix:`` is flagged
    (asserted by `test_failing_check_no_markers`).
    """
    detector = _load_detector()
    synthetic = tmp_path / "check_synthetic.py"
    synthetic.write_text(
        'REMEDIATION = "fix: do the thing to pass."\n'
        "errors: list[str] = []\n"
        'errors.append("fix: append a real correction here")\n'
    )

    assert detector._python_file_violates(synthetic) is False


def test_failing_check_no_markers(tmp_path: Path) -> None:
    """A synthetic check_*.py whose REMEDIATION lacks all three markers
    is flagged.

    Sabotage-proof inline: the next assertion adds ``run:`` to the same
    file and verifies the detector now passes — proving the failure was
    marker-driven, not a confound.
    """
    detector = _load_detector()
    synthetic = tmp_path / "check_synthetic_bad.py"
    synthetic.write_text('REMEDIATION = "Some files violate the rule. Update them."\n')

    assert detector._python_file_violates(synthetic) is True

    # Sabotage-proof: add a marker, confirm the flag clears.
    synthetic.write_text('REMEDIATION = "Some files violate the rule. run: bash fix.sh"\n')
    assert detector._python_file_violates(synthetic) is False


def test_silent_check_with_no_remediation_is_flagged(tmp_path: Path) -> None:
    """A check_*.py with zero detectable remediation text counts as a
    violation — silent checks can't bypass the rule.
    """
    detector = _load_detector()
    silent = tmp_path / "check_silent.py"
    silent.write_text("def main() -> int:\n    return 0\n")

    assert detector._python_file_violates(silent) is True


def test_error_append_with_marker_passes(tmp_path: Path) -> None:
    """``errors.append("fix: ...")`` literals satisfy F21; the same
    file without the marker fails.
    """
    detector = _load_detector()
    target = tmp_path / "check_appends.py"
    target.write_text('errors: list[str] = []\nerrors.append("fix: rename the parameter to _foo")\n')
    assert detector._python_file_violates(target) is False

    target.write_text('errors: list[str] = []\nerrors.append("Found a bad parameter. Please fix it.")\n')
    assert detector._python_file_violates(target) is True


def test_shell_remediation_marker_required(tmp_path: Path) -> None:
    """A shell check whose REMEDIATION="..." block lacks markers is
    flagged; adding ``next:`` clears it.
    """
    detector = _load_detector()
    target = tmp_path / "check-shell.sh"
    target.write_text('#!/usr/bin/env bash\nREMEDIATION="Some files break the rule. Update them please."\n')
    assert detector._shell_file_violates(target) is True

    target.write_text('#!/usr/bin/env bash\nREMEDIATION="next: re-run scripts/checks/run-all.sh"\n')
    assert detector._shell_file_violates(target) is False


def test_real_kairix_checks_all_pass() -> None:
    """The actual ``scripts/checks/check_*.{py,sh}`` files emit no
    net-new violations. Existing offenders are grandfathered in
    ``.architecture/baseline/actionable-feedback-files.txt``; the gate
    must remain green on every commit.

    Sabotage-proof: the dogfood self-scan (F21 reads its own
    REMEDIATION) means removing all three markers from
    ``check_actionable_feedback.py``'s REMEDIATION would fail this
    test. We confirm by reading the source and asserting at least one
    marker is present — that's the implicit sabotage canary.
    """
    detector = _load_detector()
    exit_code = detector.main()
    assert exit_code == 0

    # Dogfood canary — the detector's REMEDIATION must itself satisfy F21.
    rem = detector.REMEDIATION
    lowered = rem.lower()
    assert any(marker in lowered for marker in detector.ACTION_MARKERS), (
        "F21's own REMEDIATION lost its action markers — fix: restore "
        "fix:/next:/run: in scripts/checks/check_actionable_feedback.py."
    )
