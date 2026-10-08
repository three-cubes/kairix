"""``scripts/ci/eval-conversation-corpora.sh`` never passes a partial corpus result.

Black-box: the script runs with ``cwd=tmp_path`` (its corpus dir is
cwd-relative), ``OUT_DIR`` under ``tmp_path``, and ``EVAL_CMD`` pointing at a
stub that prints a SuiteResult and exits with a chosen code — no live eval,
every file under ``tmp_path``.

The shared completeness check runs after EVERY successful eval, in both the
pinned-baseline (regression-gated) and sentinel ("establishing baseline")
modes: a partial result exits 3 (inconclusive) and is never recorded as a
candidate baseline; a complete one still records / passes.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "eval-conversation-corpora.sh"

_COMPLETE: dict[str, Any] = {"suite_name": "team-alpha", "n_questions": 2, "n_passed": 2, "mean_score": 1.0}
_PARTIAL: dict[str, Any] = {**_COMPLETE, "n_questions": 1, "n_passed": 1, "judge_failures": 1}
_SENTINEL: dict[str, Any] = {"baseline": "not-yet-measured"}


def _setup(tmp_path: Path, *, baseline: dict[str, Any], result: dict[str, Any], stub_exit: int = 0) -> dict[str, str]:
    corpus = tmp_path / "reference-library" / "conversations"
    (corpus / "team-alpha").mkdir(parents=True)
    (corpus / "expected").mkdir()
    (corpus / "expected" / "team-alpha.json").write_text(json.dumps(baseline), encoding="utf-8")
    stub = tmp_path / "eval-stub.sh"
    stub.write_text(f"#!/usr/bin/env bash\necho '{json.dumps(result)}'\nexit {stub_exit}\n", encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return {
        # The interpreter running the tests first, so the script's inline
        # ``python3`` blocks can import kairix (as they do in CI).
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "OUT_DIR": str(tmp_path / "out"),
        "EVAL_CMD": str(stub),
    }


def _run(tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_SCRIPT)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_inconclusive_regression_check_fails_with_its_own_message(tmp_path: Path) -> None:
    """Pinned mode, eval exiting 3 (partial run or baseline): the gate exits 3
    with an INCONCLUSIVE error, not the "regressed" one.

    Sabotage-proof: delete the ``if [ "$eval_rc" -eq 3 ]`` branch in the
    script — the run is reported as "regressed against pinned baseline" and
    the INCONCLUSIVE assertion fails. Restored.
    """
    proc = _run(tmp_path, _setup(tmp_path, baseline=_COMPLETE, result=_PARTIAL, stub_exit=3))
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert "regression gate INCONCLUSIVE" in proc.stdout
    assert "regressed against pinned baseline" not in proc.stdout
    assert (tmp_path / "out" / "team-alpha-result.json").exists()


@pytest.mark.parametrize("baseline", [_SENTINEL, _COMPLETE], ids=["sentinel", "pinned"])
def test_partial_result_is_inconclusive_in_every_mode(tmp_path: Path, baseline: dict[str, Any]) -> None:
    """Eval exits 0 but the result is partial: the shared completeness check
    fails the gate with exit 3 — in sentinel mode too, so a partial result is
    never recorded as a candidate baseline.

    Sabotage-proof: delete the post-eval ``judge_coverage.py`` check — the
    partial result passes with exit 0 in both modes. Restored.
    """
    proc = _run(tmp_path, _setup(tmp_path, baseline=baseline, result=_PARTIAL))
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert "is PARTIAL" in proc.stdout
    assert "conversation-eval-gate: INCONCLUSIVE" in proc.stdout


@pytest.mark.parametrize("baseline", [_SENTINEL, _COMPLETE], ids=["sentinel", "pinned"])
def test_complete_result_records_successfully(tmp_path: Path, baseline: dict[str, Any]) -> None:
    """A complete result passes (pinned) / records (sentinel) with exit 0."""
    proc = _run(tmp_path, _setup(tmp_path, baseline=baseline, result=_COMPLETE))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "INCONCLUSIVE" not in proc.stdout
    assert json.loads((tmp_path / "out" / "team-alpha-result.json").read_text(encoding="utf-8")) == _COMPLETE
