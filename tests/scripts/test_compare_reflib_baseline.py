"""``scripts/compare-reflib-baseline.py`` refuses a partial committed baseline.

Runs the script as a subprocess with ``cwd=tmp_path`` (its baseline path is
cwd-relative), so every file lives under ``tmp_path``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "compare-reflib-baseline.py"


def _run(tmp_path: Path, baseline: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "benchmark-results" / "reflib-contract-baseline.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(baseline), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(_SCRIPT)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_partial_baseline_is_inconclusive(tmp_path: Path) -> None:
    """A committed baseline with judge failures exits 3 with a PARTIAL
    diagnostic; a complete one still exits 0.

    Sabotage-proof: drop the ``judge_failures(baseline)`` check in the script
    — the partial baseline is accepted and the script exits 0. Restored.
    """
    proc = _run(tmp_path, {"summary": {"weighted_total": 0.9, "judge_failures": 2}})
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert "PARTIAL" in proc.stdout

    proc = _run(tmp_path, {"summary": {"weighted_total": 0.9}})
    assert proc.returncode == 0, proc.stdout + proc.stderr
