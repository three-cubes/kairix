"""Contract tests for ``scripts/ci/benchmark_pr_comment.py`` (benchmark-gate PR comment)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "benchmark_pr_comment.py"


def _result(total: float, *, judge_failures: int = 0) -> dict[str, Any]:
    summary: dict[str, Any] = {"weighted_total": total, "category_scores": {"recall": total, "entity": total}}
    if judge_failures:
        summary["judge_failures"] = judge_failures
    return {"summary": summary}


def _build(tmp_path: Path, current: dict[str, Any], baseline: dict[str, Any], outcome: str) -> tuple[int, str]:
    cur = tmp_path / "current.json"
    base = tmp_path / "baseline.json"
    out = tmp_path / "body.txt"
    cur.write_text(json.dumps(current), encoding="utf-8")
    base.write_text(json.dumps(baseline), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), str(cur), str(base), outcome, str(out)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return proc.returncode, out.read_text(encoding="utf-8") if out.exists() else ""


@pytest.mark.parametrize("partial_side", ["current", "baseline"])
def test_partial_result_renders_only_inconclusive_diagnostic(tmp_path: Path, partial_side: str) -> None:
    """Either side partial → the body is ONLY the INCONCLUSIVE diagnostic: no
    weighted-total line, no category-delta table, no pass / fail verdict.

    Sabotage-proof: build the delta table before checking ``partial`` (the
    old inline order) — the table and weighted total appear in the body and
    the assertions fail. Restored.
    """
    current = _result(0.91, judge_failures=2 if partial_side == "current" else 0)
    baseline = _result(0.90, judge_failures=1 if partial_side == "baseline" else 0)

    rc, body = _build(tmp_path, current, baseline, "failure")
    assert rc == 0
    assert "Benchmark Gate — Contract Suite" in body  # the posting step matches on this header
    assert "Gate INCONCLUSIVE: PARTIAL result" in body
    assert f"{partial_side}: " in body
    assert "| Category |" not in body
    assert "Weighted total" not in body
    assert "Gate passed" not in body
    assert "regression detected" not in body


@pytest.mark.parametrize(("outcome", "verdict"), [("success", "Gate passed"), ("failure", "regression detected")])
def test_complete_results_keep_the_delta_table(tmp_path: Path, outcome: str, verdict: str) -> None:
    """A complete pair renders the weighted total, the category table and the
    verdict matching the compare step's outcome."""
    rc, body = _build(tmp_path, _result(0.91), _result(0.90), outcome)
    assert rc == 0
    assert "**Weighted total:** 0.9000 → 0.9100 (+0.0100)" in body
    assert "| Category | Baseline | Current | Delta |" in body
    assert "| recall | 0.9000 | 0.9100 | +0.0100 |" in body
    assert verdict in body
    assert "INCONCLUSIVE" not in body


def test_unreadable_result_writes_no_body(tmp_path: Path) -> None:
    """A missing result file writes no body (the post step falls back) and
    exits 0 so the comment step never masks the gate's own status."""
    out = tmp_path / "body.txt"
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), str(tmp_path / "missing.json"), str(tmp_path / "x.json"), "success", str(out)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0
    assert not out.exists()
    assert "Could not read result files" in proc.stderr
