"""End-to-end integration test for `kairix eval gate` (KFEAT-013, stage 5).

Exercises the real CLI dispatch path: writes a benchmark-result JSON to
disk, invokes ``main(["gate", "--result", ...])`` with no fakes, and
asserts the verdict + exit code. The gate function itself is fully unit-
covered via BDD; this test exists to prove the wiring (CLI subparser,
JSON loading, result rendering, exit code mapping) works against the
real production code path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kairix.quality.eval.cli import main as eval_cli_main

pytestmark = pytest.mark.integration


def _write_result(tmp_path: Path, weighted: float, scores: dict[str, float]) -> Path:
    """Write a benchmark-result JSON in the shape the gate expects."""
    payload = {
        "summary": {
            "weighted_total": weighted,
            "category_scores": scores,
        },
        "cases": [],  # not consulted by the gate
    }
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps(payload), encoding="utf-8")
    return result_path


def _run_gate(args: list[str]) -> int:
    """Invoke the eval CLI and return its exit code.

    The CLI dispatches via ``sys.exit(fn(args))`` so we catch SystemExit
    and read ``.value.code``. Mirrors how a shell would observe the exit
    status — closer to a real end-to-end test than reading a return value.
    """
    with pytest.raises(SystemExit) as excinfo:
        eval_cli_main(args)
    return int(excinfo.value.code or 0)


@pytest.mark.integration
def test_gate_cli_pass_exits_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """All categories above floor → PASS verdict, exit code 0."""
    result_path = _write_result(
        tmp_path,
        weighted=0.82,
        scores={
            "recall": 0.85,
            "temporal": 0.80,
            "entity": 0.85,
            "conceptual": 0.78,
            "multi_hop": 0.75,
            "procedural": 0.90,
        },
    )

    exit_code = _run_gate(["gate", "--result", str(result_path), "--floor", "0.50"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "PASS" in captured.out
    assert "Weighted total: 0.8200" in captured.out


@pytest.mark.integration
def test_gate_cli_hold_exits_two(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A weak category below floor → HOLD verdict, exit code 2."""
    result_path = _write_result(
        tmp_path,
        weighted=0.55,
        scores={
            "recall": 0.65,
            "temporal": 0.30,  # below floor
            "entity": 0.70,
            "conceptual": 0.55,
            "multi_hop": 0.55,
            "procedural": 0.55,
        },
    )

    exit_code = _run_gate(["gate", "--result", str(result_path), "--floor", "0.50"])

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "HOLD" in captured.out
    assert "temporal" in captured.out
    # Weak category prints with ✗ marker
    assert "✗" in captured.out


@pytest.mark.integration
def test_gate_cli_missing_result_exits_one(tmp_path: Path) -> None:
    """A non-existent result file is a usage error — exit code 1."""
    exit_code = _run_gate(["gate", "--result", str(tmp_path / "does-not-exist.json"), "--floor", "0.50"])
    assert exit_code == 1


@pytest.mark.integration
def test_gate_cli_empty_scores_exits_one(tmp_path: Path) -> None:
    """A result file with no category_scores is a usage error — exit code 1."""
    result_path = tmp_path / "empty-result.json"
    result_path.write_text(
        json.dumps({"summary": {"weighted_total": 0.0, "category_scores": {}}}),
        encoding="utf-8",
    )
    exit_code = _run_gate(["gate", "--result", str(result_path), "--floor", "0.50"])
    assert exit_code == 1


@pytest.mark.integration
def test_gate_cli_floor_controls_strictness(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Same result, different floors — gate flips verdict accordingly."""
    result_path = _write_result(
        tmp_path,
        weighted=0.55,
        scores={
            "recall": 0.55,
            "temporal": 0.55,
            "entity": 0.55,
            "conceptual": 0.55,
            "multi_hop": 0.55,
            "procedural": 0.55,
        },
    )

    # Strict floor: 0.60 → all 0.55 categories are weak → HOLD
    exit_code_strict = _run_gate(["gate", "--result", str(result_path), "--floor", "0.60"])
    assert exit_code_strict == 2

    capsys.readouterr()  # clear

    # Lenient floor: 0.50 → all categories pass → PASS
    exit_code_lenient = _run_gate(["gate", "--result", str(result_path), "--floor", "0.50"])
    assert exit_code_lenient == 0
