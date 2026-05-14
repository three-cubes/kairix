"""Unit tests for F23 (``scripts/checks/check_readme_coverage.py``).

F23 enforces that every top-level directory (excluding allow-listed
caches / dotfile trees) has a ``README.md`` resolver. Pre-existing
bare directories are grandfathered in
``.architecture/baseline/readme-coverage-files.txt``.

Each test has a paired sabotage-proof: introduce a bare directory in
a tmp repo and confirm the detector fires; add the README and
confirm it clears.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DETECTOR_PATH = _REPO_ROOT / "scripts" / "checks" / "check_readme_coverage.py"


def _load_detector():
    """Load the F23 detector module by file path."""
    spec = importlib.util.spec_from_file_location("_f23_detector", _DETECTOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_f23_detector"] = module
    spec.loader.exec_module(module)
    return module


def test_directory_with_readme_passes(tmp_path: Path) -> None:
    """A top-level directory that contains a ``README.md`` is not flagged.

    Sabotage-proof inline: deleting the README causes the detector to
    flag the directory.
    """
    detector = _load_detector()
    (tmp_path / "good_dir").mkdir()
    (tmp_path / "good_dir" / "README.md").write_text("ok")
    violations = detector.collect_violations(tmp_path)
    assert Path("good_dir/README.md") not in violations

    # Sabotage: remove the README, confirm it now fires.
    (tmp_path / "good_dir" / "README.md").unlink()
    violations = detector.collect_violations(tmp_path)
    assert Path("good_dir/README.md") in violations


def test_bare_directory_is_flagged(tmp_path: Path) -> None:
    """A top-level directory with no ``README.md`` is flagged.

    Sabotage-proof inline: adding the README clears the flag.
    """
    detector = _load_detector()
    (tmp_path / "bare_dir").mkdir()
    violations = detector.collect_violations(tmp_path)
    assert Path("bare_dir/README.md") in violations

    # Sabotage: add the README, confirm the flag clears.
    (tmp_path / "bare_dir" / "README.md").write_text("ok")
    violations = detector.collect_violations(tmp_path)
    assert Path("bare_dir/README.md") not in violations


def test_allowlisted_directories_pass(tmp_path: Path) -> None:
    """Allow-listed directories (caches, .git internals, dotfiles)
    don't require a README.
    """
    detector = _load_detector()
    for name in (".git", ".github", ".pytest_cache", ".architecture", "__pycache__", "htmlcov"):
        (tmp_path / name).mkdir()
    violations = detector.collect_violations(tmp_path)
    assert violations == set()


def test_dotfile_directories_pass_silently(tmp_path: Path) -> None:
    """Any directory whose name starts with ``.`` is exempt — the
    catch-all dotfile rule.
    """
    detector = _load_detector()
    (tmp_path / ".secret_config").mkdir()
    (tmp_path / ".idea").mkdir()
    violations = detector.collect_violations(tmp_path)
    assert violations == set()


def test_files_at_top_level_are_ignored(tmp_path: Path) -> None:
    """The rule applies to *directories*, not files. README.md is
    only required where a directory exists.
    """
    detector = _load_detector()
    (tmp_path / "Makefile").write_text("all:")
    (tmp_path / "README.md").write_text("# repo")
    violations = detector.collect_violations(tmp_path)
    assert violations == set()


def test_is_exempt_recognises_well_known_names() -> None:
    """The allow-list contains the standard cache / hidden directories."""
    detector = _load_detector()
    assert detector._is_exempt(".git") is True
    assert detector._is_exempt(".github") is True
    assert detector._is_exempt("__pycache__") is True
    assert detector._is_exempt(".architecture") is True
    assert detector._is_exempt(".claude") is True
    assert detector._is_exempt(".any_dotfile_dir") is True
    assert detector._is_exempt("kairix") is False
    assert detector._is_exempt("tests") is False


def test_real_repo_readme_coverage_gate_is_green() -> None:
    """The real ``scripts/checks/check_readme_coverage.py`` run against
    the full repo emits no net-new violations. Pre-existing bare
    directories are grandfathered in
    ``.architecture/baseline/readme-coverage-files.txt``.
    """
    detector = _load_detector()
    assert detector.main() == 0


def test_remediation_carries_action_markers() -> None:
    """F23's own REMEDIATION must satisfy F21 — the agent reading a
    README-coverage failure should get the correction action inline.
    """
    detector = _load_detector()
    rem = detector.REMEDIATION.lower()
    assert "fix:" in rem
    assert "next:" in rem
    assert "run:" in rem
