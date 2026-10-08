"""LoCoMo nightly scripts reject partial (judge-failure) SuiteResults.

Black-box tests of ``scripts/ci/locomo-nightly-run.sh`` and
``scripts/ci/locomo-nightly-compare.sh`` plus their shared helper
``scripts/ci/judge_coverage.py``. External tools are replaced by fake
executables on ``PATH`` (no kairix internals are patched):

* ``python3`` — answers ``python3 -m kairix.cli eval`` with a canned
  SuiteResult and forwards every other call to the real interpreter;
* ``gh`` — lists one prior run and "downloads" a canned prior artifact.

Every scratch file lives under ``tmp_path``.
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

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CI_DIR = _REPO_ROOT / "scripts" / "ci"


def _suite_result(*, judge_failures: int = 0, n_passed: int = 2) -> dict[str, Any]:
    rows: list[dict[str, Any]] = [{"category": "single-hop", "score": 1.0, "pass": True}] * n_passed
    rows += [{"category": "single-hop", "score": None, "pass": None, "judge_failure": "backend_error"}] * judge_failures
    return {
        "suite_name": "locomo",
        "n_questions": n_passed,
        "n_passed": n_passed,
        "mean_score": 1.0,
        "per_category": {"single-hop": {"n": float(n_passed), "passed": float(n_passed), "mean": 1.0}},
        "rows": rows,
        "judge_failures": judge_failures,
    }


def _write_exe(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _fake_bin(tmp_path: Path, *, eval_json: Path | None = None, prior_json: Path | None = None) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _write_exe(
        bin_dir / "python3",
        "#!/usr/bin/env bash\n"
        'if [ "${1:-}" = "-m" ] && [ "${2:-}" = "kairix.cli" ]; then\n'
        f'    cat "{eval_json}"\n'
        "    exit 0\n"
        "fi\n"
        f'exec "{sys.executable}" "$@"\n',
    )
    if prior_json is not None:
        _write_exe(
            bin_dir / "gh",
            "#!/usr/bin/env bash\n"
            'if [ "$1 $2" = "run list" ]; then echo 4242; exit 0; fi\n'
            'if [ "$1 $2" = "run download" ]; then\n'
            '    while [ "$#" -gt 0 ]; do\n'
            '        if [ "$1" = "--dir" ]; then dir="$2"; fi\n'
            "        shift\n"
            "    done\n"
            f'    cp "{prior_json}" "$dir/locomo-nightly-20260101.json"\n'
            "    exit 0\n"
            "fi\n"
            "exit 1\n",
        )
    return bin_dir


def _run(script: str, *, tmp_path: Path, bin_dir: Path, extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        **extra_env,
    }
    return subprocess.run(
        ["bash", str(_CI_DIR / script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _dump(path: Path, data: dict[str, Any]) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# locomo-nightly-run.sh
# ---------------------------------------------------------------------------


def test_nightly_run_fails_on_partial_result_before_publishing_artifacts(tmp_path: Path) -> None:
    """A SuiteResult with judge failures fails the run, writes no JSON/CSV
    under ./artifacts/, and exports nothing to GITHUB_ENV. A fully judged
    result still publishes both artifacts.

    Sabotage-proof: delete the ``judge_coverage.py`` check in
    ``locomo-nightly-run.sh`` — the partial run exits 0 and publishes the
    CSV, so the first assertions fail. Restored.
    """
    partial = _dump(tmp_path / "partial.json", _suite_result(judge_failures=1))
    github_env = tmp_path / "github_env"
    github_env.write_text("", encoding="utf-8")

    proc = _run(
        "locomo-nightly-run.sh",
        tmp_path=tmp_path,
        bin_dir=_fake_bin(tmp_path, eval_json=partial),
        extra_env={"GITHUB_ENV": str(github_env)},
    )
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert "is PARTIAL" in proc.stdout
    assert "fix:" in proc.stdout
    assert list((tmp_path / "artifacts").iterdir()) == []
    assert github_env.read_text(encoding="utf-8") == ""

    complete = _dump(tmp_path / "complete.json", _suite_result())
    proc = _run(
        "locomo-nightly-run.sh",
        tmp_path=tmp_path,
        bin_dir=_fake_bin(tmp_path, eval_json=complete),
        extra_env={"GITHUB_ENV": str(github_env)},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    published = sorted(p.suffix for p in (tmp_path / "artifacts").iterdir())
    assert published == [".csv", ".json"]
    assert "JSON_PATH=" in github_env.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# locomo-nightly-compare.sh
# ---------------------------------------------------------------------------


def test_nightly_compare_rejects_partial_current_result(tmp_path: Path) -> None:
    """A partial current SuiteResult fails the compare step before any
    prior artifact is fetched.

    Sabotage-proof: delete the current-result ``judge_coverage.py`` check in
    ``locomo-nightly-compare.sh`` — the step proceeds, compares against the
    (complete) prior and exits 0. Restored.
    """
    current = _dump(tmp_path / "current.json", _suite_result(judge_failures=2))
    prior = _dump(tmp_path / "prior.json", _suite_result())

    proc = _run(
        "locomo-nightly-compare.sh",
        tmp_path=tmp_path,
        bin_dir=_fake_bin(tmp_path, prior_json=prior),
        extra_env={"JSON_PATH": str(current)},
    )
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert "current LoCoMo nightly" in proc.stdout
    assert "is PARTIAL" in proc.stdout


def test_nightly_compare_rejects_partial_prior_artifact(tmp_path: Path) -> None:
    """A partial prior artifact is not used as the comparison baseline: the
    step fails naming the prior run. With a complete prior and no
    regression, the step passes without posting a comment.

    Sabotage-proof: delete the prior-artifact ``judge_coverage.py`` check in
    ``locomo-nightly-compare.sh`` — the partial prior is compared, the step
    exits 0, and the first assertion fails. Restored.
    """
    current = _dump(tmp_path / "current.json", _suite_result())
    partial_prior = _dump(tmp_path / "prior.json", _suite_result(judge_failures=1))

    proc = _run(
        "locomo-nightly-compare.sh",
        tmp_path=tmp_path,
        bin_dir=_fake_bin(tmp_path, prior_json=partial_prior),
        extra_env={"JSON_PATH": str(current)},
    )
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert "prior LoCoMo nightly (run 4242)" in proc.stdout

    complete_prior = _dump(tmp_path / "prior.json", _suite_result())
    proc = _run(
        "locomo-nightly-compare.sh",
        tmp_path=tmp_path,
        bin_dir=_fake_bin(tmp_path, prior_json=complete_prior),
        extra_env={"JSON_PATH": str(current)},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "No regression" in proc.stdout


# ---------------------------------------------------------------------------
# judge_coverage.py
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected_rc"),
    [
        (_suite_result(), 0),
        (_suite_result(judge_failures=1), 3),
        # Row-level failure without the summary count still counts.
        ({**_suite_result(judge_failures=1), "judge_failures": 0}, 3),
        # Pre-judge_failures artifacts carry no count and are treated as complete.
        ({"suite_name": "locomo", "n_questions": 1, "n_passed": 1, "mean_score": 1.0, "rows": []}, 0),
    ],
    ids=["complete", "counted", "row_only", "legacy"],
)
def test_judge_coverage_helper_exit_codes(tmp_path: Path, data: dict[str, Any], expected_rc: int) -> None:
    """The helper exits 3 (shared inconclusive) for any judge failure —
    summary count or failed row — via ``kairix.quality.completeness``.

    Sabotage-proof: drop the ``_failed_rows(...)`` terms from
    ``completeness.judge_failures`` — the ``row_only`` case exits 0 and fails.
    Restored.
    """
    path = _dump(tmp_path / "result.json", data)
    proc = subprocess.run(
        [sys.executable, str(_CI_DIR / "judge_coverage.py"), "test", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == expected_rc, proc.stdout


def test_judge_coverage_helper_rejects_unreadable_result(tmp_path: Path) -> None:
    """A missing / non-JSON result fails with an affordance instead of passing."""
    proc = subprocess.run(
        [sys.executable, str(_CI_DIR / "judge_coverage.py"), "test", str(tmp_path / "missing.json")],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 1
    assert "unreadable" in proc.stdout
    assert "fix:" in proc.stdout
