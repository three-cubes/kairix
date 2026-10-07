"""Structural tests for SGO-105 — ``make check`` == the literal CI gate.

CI Stage 2 (``.github/workflows/ci.yml`` job ``unit-and-type``) runs the
unit+bdd+contract suite with a repo coverage floor (``--cov-fail-under=80``)
AND the F7 per-file coverage floor (``check_per_file_coverage.py``). Before
SGO-105 ``make check`` ran the weaker ``test-all`` target — bare ``pytest``
with no ``--cov``/``--cov-fail-under``/F7 — so a contributor's green
``make check`` could still fail CI on coverage/F7. These tests lock the
parity in place so it cannot silently regress back to the weak subset.

Sabotage-proof (inline): reverting ``check`` to depend on ``test-all`` (or
dropping ``--cov-fail-under``/the F7 invocation from ``test-ci``) makes
``test_check_target_runs_ci_coverage_gate`` fail; deleting/altering
``.python-version`` makes ``test_python_version_pinned_to_ci_runtime`` fail.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MAKEFILE = _REPO_ROOT / "Makefile"
_PYTHON_VERSION_FILE = _REPO_ROOT / ".python-version"

# The CI Stage 2 python runtime matrix is 3.13-only: the tc_fitness gate engine
# requires 3.13+ (v0.17 dropped 3.12; the kairix wheel still supports >=3.12). A
# pinned ``.python-version`` makes ``uv``/pyenv select the same interpreter locally.
_CI_PYTHON = "3.13"


def _prerequisites_of(target: str, makefile_text: str) -> list[str]:
    """Return the prerequisite names declared for a Make ``target``."""
    match = re.search(rf"(?m)^{re.escape(target)}:\s*(.*)$", makefile_text)
    assert match is not None, f"Makefile has no '{target}:' target"
    return match.group(1).split()


def _recipe_of(target: str, makefile_text: str) -> str:
    """Return the tab-indented recipe body for a Make ``target``.

    A recipe runs from the target line until the first line that is neither
    blank, a comment, nor tab-indented (i.e. the next target/declaration).
    """
    lines = makefile_text.splitlines()
    start = next(i for i, line in enumerate(lines) if re.match(rf"^{re.escape(target)}:", line))
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("\t") or line.startswith("#") or line.strip() == "":
            body.append(line)
            continue
        break
    return "\n".join(body)


def test_check_target_runs_ci_coverage_gate() -> None:
    """``make check`` must reach the CI coverage floor + F7, not ``test-all``.

    Encodes SGO-105: ``check`` depends on ``test-ci`` (the literal CI Stage 2
    command), whose recipe carries the repo ``--cov-fail-under`` floor and the
    F7 per-file ``check_per_file_coverage.py`` invocation.
    """
    text = _MAKEFILE.read_text(encoding="utf-8")

    check_prereqs = _prerequisites_of("check", text)
    assert "test-ci" in check_prereqs, (
        f"`make check` must depend on `test-ci` (the literal CI Stage 2 gate), got prerequisites: {check_prereqs}"
    )
    assert "test-all" not in check_prereqs, (
        "`make check` must NOT depend on the weaker `test-all` (bare pytest, "
        "no --cov/--cov-fail-under/F7) — that reopens the SGO-105 parity gap"
    )

    recipe = _recipe_of("test-ci", text)
    assert "--cov=kairix" in recipe, "`test-ci` must measure coverage (--cov=kairix)"
    assert "--cov-fail-under=80" in recipe, "`test-ci` must enforce the 80% repo coverage floor CI Stage 2 enforces"
    assert "unit or bdd or contract" in recipe, "`test-ci` must run the same CI Stage 2 selector"
    assert "check_per_file_coverage.py" in recipe, "`test-ci` must run the F7 per-file coverage floor CI Stage 2 runs"


def test_python_version_pinned_to_ci_runtime() -> None:
    """A pinned ``.python-version`` selects the CI runtime (3.13) locally."""
    assert _PYTHON_VERSION_FILE.exists(), ".python-version must exist so uv/pyenv pin the CI interpreter locally"
    pinned = _PYTHON_VERSION_FILE.read_text(encoding="utf-8").strip()
    assert pinned.startswith(_CI_PYTHON), f".python-version must pin the CI runtime {_CI_PYTHON}, got '{pinned}'"
