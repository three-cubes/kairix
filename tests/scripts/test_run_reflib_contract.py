"""``scripts/run-reflib-contract.py`` refuses a partial run (exit 3)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from kairix.quality.benchmark.runner import BenchmarkResult

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "run-reflib-contract.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("run_reflib_contract_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_reflib_contract_under_test"] = mod
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]  # importlib stubs omit exec_module
    return mod


def _runner(summary: dict[str, Any]) -> Any:
    def _run(_suite: Any, **_kwargs: Any) -> BenchmarkResult:
        return BenchmarkResult(meta={}, summary=summary, diagnostics={}, cases=[])

    return _run


def test_partial_run_exits_inconclusive(capsys: pytest.CaptureFixture[str]) -> None:
    """A run with judge failures exits 3 with a PARTIAL diagnostic, even when
    its partial weighted total would clear the floor; a complete run above
    the floor still exits 0 and one below it exits 1.

    Sabotage-proof: drop the ``judge_failures(result)`` check in ``main`` —
    the partial run passes the floor and exits 0. Restored.
    """
    mod = _load()
    rc = mod.main(run=_runner({"weighted_total": 0.95, "category_scores": {}, "judge_failures": 2}))
    assert rc == 3
    assert "PARTIAL" in capsys.readouterr().out

    assert mod.main(run=_runner({"weighted_total": 0.95, "category_scores": {}})) == 0
    assert mod.main(run=_runner({"weighted_total": 0.10, "category_scores": {}})) == 1
