"""``scripts/ci/eval-conversation-corpora.sh`` reports an inconclusive
regression check (eval exit 3) as its own failure.

Black-box: the script runs with ``cwd=tmp_path`` (its corpus dir is
cwd-relative), ``OUT_DIR`` under ``tmp_path``, and ``EVAL_CMD`` pointing at
a stub that prints a SuiteResult and exits with a chosen code — no live
eval, every file under ``tmp_path``.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "eval-conversation-corpora.sh"


def _setup(tmp_path: Path, *, stub_exit: int) -> dict[str, str]:
    corpus = tmp_path / "reference-library" / "conversations"
    (corpus / "team-alpha").mkdir(parents=True)
    (corpus / "expected").mkdir()
    (corpus / "expected" / "team-alpha.json").write_text(
        json.dumps({"suite_name": "team-alpha", "n_questions": 2, "n_passed": 2, "mean_score": 1.0}),
        encoding="utf-8",
    )
    stub = tmp_path / "eval-stub.sh"
    result = {"suite_name": "team-alpha", "n_questions": 1, "n_passed": 1, "mean_score": 1.0, "judge_failures": 1}
    stub.write_text(
        f"#!/usr/bin/env bash\necho '{json.dumps(result)}'\nexit {stub_exit}\n",
        encoding="utf-8",
    )
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
    """Eval exiting 3 (partial run or baseline) fails the gate with an
    INCONCLUSIVE error, not the "regressed" one, and outputs stay under
    ``OUT_DIR``.

    Sabotage-proof: delete the ``if [ "$eval_rc" -eq 3 ]`` branch in the
    script — the run is reported as "regressed against pinned baseline" and
    the INCONCLUSIVE assertion fails. Restored.
    """
    proc = _run(tmp_path, _setup(tmp_path, stub_exit=3))
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "regression gate INCONCLUSIVE" in proc.stdout
    assert "regressed against pinned baseline" not in proc.stdout
    assert (tmp_path / "out" / "team-alpha-result.json").exists()


def test_passing_regression_check_still_passes(tmp_path: Path) -> None:
    """Eval exiting 0 passes the gate; the per-corpus summary still flags the
    stub's partial result with a PARTIAL warning (display-only)."""
    proc = _run(tmp_path, _setup(tmp_path, stub_exit=0))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PARTIAL RESULT" in proc.stdout
