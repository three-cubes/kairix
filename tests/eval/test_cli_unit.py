"""Unit tests for the kairix.quality.eval CLI dispatch and per-command bodies.

The eval subcommands all delegate to helper modules (SuiteGenerator,
GoldBuilder, run_monitor, ...). These tests drive each command through
``main(argv, deps=EvalCliDeps(...))`` with the helpers injected as
stand-ins (F1-clean — no patching of kairix modules) so the CLI's
parsing + result-mapping logic exercises end-to-end without touching
Azure, hybrid search, or the real index.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import kairix.quality.eval.cli as eval_cli
from kairix.quality.eval.cli import EvalCliDeps

pytestmark = pytest.mark.unit


def _drive(args: list[str], deps: EvalCliDeps | None = None) -> tuple[str, str, int]:
    out, err = io.StringIO(), io.StringIO()
    code = 0
    try:
        with redirect_stdout(out), redirect_stderr(err):
            eval_cli.main(args, deps=deps)
    except SystemExit as e:
        code = int(e.code) if e.code is not None else 0
    return out.getvalue(), err.getvalue(), code


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------


class _FakeSuiteGenerator:
    """SuiteGenerator stand-in. Capture inputs and return a known result."""

    def __init__(self, *, generate_result: Any = None, enrich_result: Any = None) -> None:
        self._gen_result = generate_result
        self._enr_result = enrich_result
        self.generate_calls: list[dict] = []
        self.enrich_calls: list[dict] = []

    def generate_suite(self, **kw: Any) -> Any:
        self.generate_calls.append(kw)
        return self._gen_result

    def enrich_suite(self, **kw: Any) -> Any:
        self.enrich_calls.append(kw)
        return self._enr_result


def _gen_result(**kw: Any) -> Any:
    defaults: dict[str, Any] = {
        "calibration_passed": True,
        "n_accepted": 10,
        "n_rejected": 2,
        "n_failed": 1,
        "category_counts": {"recall": 5, "entity": 5},
        "errors": [],
        "output_path": "/tmp/out.yaml",
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def test_generate_success_path(tmp_path: Path) -> None:

    fake_gen = _FakeSuiteGenerator(generate_result=_gen_result())
    deps = EvalCliDeps(new_suite_generator=lambda: fake_gen)

    out_path = tmp_path / "out.yaml"
    stdout, _stderr, code = _drive(
        ["generate", "--output", str(out_path), "--count", "5", "--categories", "recall,entity"], deps
    )
    assert code == 0
    assert "Accepted: 10" in stdout
    assert "Rejected (no grade-2 doc): 2" in stdout
    assert len(fake_gen.generate_calls) >= 1, "expected generate to be called at least once"
    assert fake_gen.generate_calls[0]["n_cases"] == 5
    assert fake_gen.generate_calls[0]["categories"] == ["recall", "entity"]


def test_generate_calibration_failure_exits_1(tmp_path: Path) -> None:

    failing = _gen_result(calibration_passed=False, errors=["anchor-1 mismatch"])
    deps = EvalCliDeps(new_suite_generator=lambda: _FakeSuiteGenerator(generate_result=failing))

    _stdout, stderr, code = _drive(["generate", "--output", str(tmp_path / "x.yaml")], deps)
    assert code == 1
    assert "Calibration failed" in stderr
    assert "anchor-1 mismatch" in stderr


def test_generate_with_warnings(tmp_path: Path) -> None:

    with_warn = _gen_result(errors=["minor: no doc for q1"])
    deps = EvalCliDeps(new_suite_generator=lambda: _FakeSuiteGenerator(generate_result=with_warn))

    stdout, _stderr, code = _drive(["generate", "--output", str(tmp_path / "x.yaml")], deps)
    assert code == 0
    assert "Warnings" in stdout
    assert "minor: no doc for q1" in stdout


def test_generate_no_calibrate_flag_bypasses_calibration_check(tmp_path: Path) -> None:
    """When --no-calibrate is passed, calibration_passed=False does NOT exit 1."""

    failing = _gen_result(calibration_passed=False)
    deps = EvalCliDeps(new_suite_generator=lambda: _FakeSuiteGenerator(generate_result=failing))

    _stdout, _stderr, code = _drive(["generate", "--output", str(tmp_path / "x.yaml"), "--no-calibrate"], deps)
    assert code == 0


def test_generate_db_path_defaults_to_deployment_index(tmp_path: Path) -> None:
    """No --db -> generate_suite gets the deployment's index, not ~/.cache (#552).

    Pre-fix the CLI defaulted --db to ~/.cache/kairix/index.sqlite, so on a
    standard docker/system install (index at /var/lib/kairix/index.sqlite) every
    invocation needed a manual --db override. The handler now resolves via
    kairix.paths.index_path() — the same mode-aware path the worker writes.

    Sabotage-proven: reverting the db-path resolver to the hardcoded ~/.cache
    string makes the captured db_path != str(index_path()) and fails the first assert.
    """
    from kairix.paths import index_path

    fake_gen = _FakeSuiteGenerator(generate_result=_gen_result())
    deps = EvalCliDeps(new_suite_generator=lambda: fake_gen)

    # No --db -> the deployment's canonical index for the current mode.
    _drive(["generate", "--output", str(tmp_path / "o.yaml")], deps)
    assert fake_gen.generate_calls[-1]["db_path"] == str(index_path())

    # An explicit --db is respected verbatim.
    _drive(["generate", "--output", str(tmp_path / "o2.yaml"), "--db", "/custom/index.sqlite"], deps)
    assert fake_gen.generate_calls[-1]["db_path"] == "/custom/index.sqlite"


# ---------------------------------------------------------------------------
# enrich
# ---------------------------------------------------------------------------


def test_enrich_command_outputs_summary(tmp_path: Path) -> None:

    enrich_result = SimpleNamespace(
        n_cases=5,
        n_enriched=4,
        n_skipped=1,
        n_failed=0,
        errors=[],
        output_path="/tmp/enriched.yaml",
    )
    deps = EvalCliDeps(new_suite_generator=lambda: _FakeSuiteGenerator(enrich_result=enrich_result))

    suite_in = tmp_path / "in.yaml"
    suite_in.write_text("cases:\n", encoding="utf-8")
    stdout, _stderr, code = _drive(["enrich", "--suite", str(suite_in), "--output", str(tmp_path / "out.yaml")], deps)
    assert code == 0
    assert "Total cases: 5" in stdout
    assert "Enriched with gold_titles: 4" in stdout


def test_enrich_with_errors_prints_warnings(tmp_path: Path) -> None:

    enrich_result = SimpleNamespace(
        n_cases=3,
        n_enriched=2,
        n_skipped=1,
        n_failed=0,
        errors=["case-3 fell back"],
        output_path="/tmp/enriched.yaml",
    )
    deps = EvalCliDeps(new_suite_generator=lambda: _FakeSuiteGenerator(enrich_result=enrich_result))
    suite_in = tmp_path / "in.yaml"
    suite_in.write_text("cases:\n", encoding="utf-8")
    stdout, _stderr, _ = _drive(["enrich", "--suite", str(suite_in), "--output", str(tmp_path / "out.yaml")], deps)
    assert "Warnings" in stdout
    assert "case-3 fell back" in stdout


# ---------------------------------------------------------------------------
# monitor
# ---------------------------------------------------------------------------


def test_monitor_no_regression(tmp_path: Path) -> None:

    result = SimpleNamespace(
        ts="2026-05-14T00:00:00Z",
        n_cases=10,
        weighted_ndcg=0.72,
        vec_failed_count=0,
        ndcg_by_category={"recall": 0.8, "entity": 0.7},
        regression=False,
        regression_detail="",
    )
    deps = EvalCliDeps(run_monitor=lambda **kw: result)

    suite = tmp_path / "canary.yaml"
    suite.write_text("cases:\n", encoding="utf-8")
    stdout, _stderr, code = _drive(["monitor", "--suite", str(suite), "--log", str(tmp_path / "mon.jsonl")], deps)
    assert code == 0
    assert "Weighted NDCG: 0.7200" in stdout
    assert "No regression detected" in stdout


def test_monitor_regression_returns_2(tmp_path: Path) -> None:

    result = SimpleNamespace(
        ts="2026-05-14T00:00:00Z",
        n_cases=10,
        weighted_ndcg=0.5,
        vec_failed_count=2,
        ndcg_by_category={"recall": 0.5},
        regression=True,
        regression_detail="-7.5% vs 7-day avg",
    )
    deps = EvalCliDeps(run_monitor=lambda **kw: result)

    suite = tmp_path / "canary.yaml"
    suite.write_text("cases:\n", encoding="utf-8")
    _stdout, stderr, code = _drive(["monitor", "--suite", str(suite), "--log", str(tmp_path / "mon.jsonl")], deps)
    assert code == 2
    assert "REGRESSION DETECTED" in stderr


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def test_report_writes_to_stdout_when_no_output(tmp_path: Path) -> None:

    deps = EvalCliDeps(generate_report=lambda **kw: "# Report body")

    stdout, _stderr, code = _drive(["report", "--days", "7", "--log", str(tmp_path / "mon.jsonl")], deps)
    assert code == 0
    assert "# Report body" in stdout


def test_report_writes_to_file_when_output_set(tmp_path: Path) -> None:

    deps = EvalCliDeps(generate_report=lambda **kw: "# Report")

    out = tmp_path / "report.md"
    stdout, _stderr, code = _drive(["report", "--output", str(out), "--log", str(tmp_path / "mon.jsonl")], deps)
    assert code == 0
    assert "Report written to" in stdout
    assert out.read_text(encoding="utf-8") == "# Report"


def test_report_exits_1_when_parent_dir_missing(tmp_path: Path) -> None:

    deps = EvalCliDeps(generate_report=lambda **kw: "x")
    out = tmp_path / "nope" / "deep" / "report.md"
    _stdout, stderr, code = _drive(["report", "--output", str(out), "--log", str(tmp_path / "mon.jsonl")], deps)
    assert code == 1
    assert "does not exist" in stderr


# ---------------------------------------------------------------------------
# build-gold
# ---------------------------------------------------------------------------


class _FakeGoldBuilder:
    def __init__(self, report: Any) -> None:
        self._report = report
        self.calls: list[dict] = []

    def build_independent_gold(self, **kw: Any) -> Any:
        self.calls.append(kw)
        return self._report


def test_build_gold_outputs_report_summary(tmp_path: Path) -> None:

    report = SimpleNamespace(
        queries_processed=5,
        total_candidates_pooled=40,
        avg_candidates_per_query=8.0,
        total_judge_calls=10,
        grade_distribution={2: 4, 1: 3, 0: 3},
    )
    deps = EvalCliDeps(new_gold_builder=lambda: _FakeGoldBuilder(report))

    suite_in = tmp_path / "suite.yaml"
    suite_in.write_text("cases:\n", encoding="utf-8")
    stdout, _stderr, code = _drive(
        [
            "build-gold",
            "--suite",
            str(suite_in),
            "--output",
            str(tmp_path / "gold.yaml"),
            "--judge-runs",
            "1",
        ],
        deps,
    )
    assert code == 0
    assert "Queries: 5" in stdout
    assert "Grades: 2=4 1=3 0=3" in stdout


# ---------------------------------------------------------------------------
# hybrid-sweep
# ---------------------------------------------------------------------------


def test_hybrid_sweep_with_best_config(tmp_path: Path) -> None:

    cfg = SimpleNamespace(
        name="best-config",
        mode="rrf",
        rrf_k=60,
        entity_enabled=True,
        entity_factor=1.2,
        entity_cap=5,
        procedural_enabled=False,
        procedural_factor=1.0,
        bm25_limit=20,
        vec_limit=20,
    )
    best = SimpleNamespace(
        config=cfg,
        weighted_total=0.81,
        ndcg_at_10=0.79,
        hit_at_5=0.66,
        mrr_at_10=0.72,
        n_vec_failed=0,
        n_cases=10,
        avg_latency_ms=120.0,
    )
    report = SimpleNamespace(
        total_configs=3,
        total_duration_s=12.5,
        best=best,
        results=[best],
    )
    deps = EvalCliDeps(hybrid_configs=lambda: [cfg], sweep_hybrid=lambda **kw: report)

    suite_in = tmp_path / "g.yaml"
    suite_in.write_text("cases:\n", encoding="utf-8")
    stdout, _stderr, code = _drive(
        [
            "hybrid-sweep",
            "--suite",
            str(suite_in),
            "--output",
            str(tmp_path / "sweep.csv"),
            "--collection",
            "shared",
            "--quick",
        ],
        deps,
    )
    assert code == 0
    assert "BEST CONFIG" in stdout
    assert "best-config" in stdout
    assert "Full results" in stdout


def test_hybrid_sweep_without_best(tmp_path: Path) -> None:
    """When report.best is None the BEST CONFIG block is suppressed."""

    deps = EvalCliDeps(
        hybrid_configs=lambda: [],
        sweep_hybrid=lambda **kw: SimpleNamespace(total_configs=0, total_duration_s=0.0, best=None, results=[]),
    )

    suite_in = tmp_path / "g.yaml"
    suite_in.write_text("cases:\n", encoding="utf-8")
    stdout, _stderr, code = _drive(["hybrid-sweep", "--suite", str(suite_in)], deps)
    assert code == 0
    assert "BEST CONFIG" not in stdout


# ---------------------------------------------------------------------------
# auto-gold
# ---------------------------------------------------------------------------


def test_auto_gold_writes_suite(tmp_path: Path) -> None:

    class _Db:
        def close(self):
            """Test stub — exercises the close() path; no-op for fake DB."""

    profile = SimpleNamespace(
        total_docs=100,
        collections=["docs", "shared"],
        procedural_count=12,
        date_filename_count=5,
        entity_doc_count=8,
    )
    written: list[tuple] = []

    def _build_suite(queries: list, path: str) -> None:
        written.append((len(queries), path))

    deps = EvalCliDeps(
        index_db_path=lambda: str(tmp_path / "k.db"),
        open_db=lambda p: _Db(),
        analyse_corpus=lambda db: profile,
        template_queries=lambda p, n: [{"category": "recall"}, {"category": "entity"}, {"category": "recall"}],
        build_suite=_build_suite,
    )

    output = tmp_path / "auto.yaml"
    stdout, _stderr, code = _drive(["auto-gold", "--output", str(output), "--count", "3"], deps)
    assert code == 0
    assert "Generated 3 evaluation queries" in stdout
    assert written == [(3, str(output))]


# ---------------------------------------------------------------------------
# tune (one more branch)
# ---------------------------------------------------------------------------


def test_tune_with_index_unavailable_falls_back(tmp_path: Path) -> None:
    """The except branch around analyse_corpus / open_db must not raise."""

    result_file = tmp_path / "r.json"
    result_file.write_text(json.dumps({"summary": {"category_scores": {"recall": 0.1}}}), encoding="utf-8")

    def _explode() -> Any:
        raise RuntimeError("no index")

    deps = EvalCliDeps(index_db_path=_explode)
    stdout, _stderr, code = _drive(["tune", "--result", str(result_file), "--floor", "0.5"], deps)
    assert code == 0
    assert "index not available" in stdout
    assert "Weak categories: recall" in stdout


def test_tune_no_categories_in_result(tmp_path: Path) -> None:
    result_file = tmp_path / "r.json"
    result_file.write_text(json.dumps({"summary": {}}), encoding="utf-8")
    _stdout, stderr, code = _drive(["tune", "--result", str(result_file)])
    assert code == 1
    assert "No category_scores" in stderr


def test_tune_returns_recommendations_when_corpus_hints_available(tmp_path: Path) -> None:
    """When corpus hints are available, recommend() is called for the weak categories."""

    result_file = tmp_path / "r.json"
    result_file.write_text(json.dumps({"summary": {"category_scores": {"recall": 0.1}}}), encoding="utf-8")

    class _Db:
        def close(self) -> None:
            """Test stub — exercises the close() path; no-op for fake DB."""

    deps = EvalCliDeps(
        index_db_path=lambda: str(tmp_path / "k.db"),
        open_db=lambda p: _Db(),
        analyse_corpus=lambda db: SimpleNamespace(procedural_count=1, date_filename_count=1, entity_doc_count=1),
    )

    stdout, _stderr, code = _drive(["tune", "--result", str(result_file)], deps)
    assert code == 0
    # Either Recommendations or 'No specific recommendations' — both must
    # come from the success path (no 'index not available' fallback).
    assert "index not available" not in stdout


# ---------------------------------------------------------------------------
# gate
# ---------------------------------------------------------------------------


def test_gate_pass(tmp_path: Path) -> None:

    result_file = tmp_path / "r.json"
    result_file.write_text(
        json.dumps(
            {
                "summary": {
                    "category_scores": {"recall": 0.8},
                    "weighted_total": 0.78,
                }
            }
        ),
        encoding="utf-8",
    )

    deps = EvalCliDeps(run_gate=lambda scores, **kw: SimpleNamespace(passed=True, format=lambda: "GATE-PASS"))
    stdout, _stderr, code = _drive(["gate", "--result", str(result_file)], deps)
    assert code == 0
    assert "GATE-PASS" in stdout


def test_gate_hold(tmp_path: Path) -> None:

    result_file = tmp_path / "r.json"
    result_file.write_text(
        json.dumps(
            {
                "summary": {
                    "category_scores": {"recall": 0.3},
                    "weighted_total": 0.3,
                }
            }
        ),
        encoding="utf-8",
    )
    deps = EvalCliDeps(run_gate=lambda scores, **kw: SimpleNamespace(passed=False, format=lambda: "GATE-HOLD"))
    _stdout, _stderr, code = _drive(["gate", "--result", str(result_file)], deps)
    assert code == 2


def test_gate_missing_file(tmp_path: Path) -> None:
    _stdout, stderr, code = _drive(["gate", "--result", str(tmp_path / "absent.json")])
    assert code == 1
    assert "ERROR" in stderr


def test_gate_missing_scores(tmp_path: Path) -> None:
    bad = tmp_path / "r.json"
    bad.write_text(json.dumps({"summary": {}}), encoding="utf-8")
    _stdout, stderr, code = _drive(["gate", "--result", str(bad)])
    assert code == 1
    assert "No category_scores" in stderr


# ---------------------------------------------------------------------------
# sweep
# ---------------------------------------------------------------------------


def test_sweep_command_runs(tmp_path: Path) -> None:

    best = SimpleNamespace(
        weights=(1.0, 2.0, 3.0),
        query_style="natural",
        weighted_total=0.71,
        ndcg_at_10=0.69,
        hit_at_5=0.60,
        mrr_at_10=0.65,
    )
    report = SimpleNamespace(
        total_configs=2,
        total_duration_s=5.0,
        best=best,
        results=[best],
    )
    deps = EvalCliDeps(sweep_bm25=lambda **kw: report)

    suite_in = tmp_path / "s.yaml"
    suite_in.write_text("cases:\n", encoding="utf-8")
    stdout, _stderr, code = _drive(["sweep", "--suite", str(suite_in), "--output", str(tmp_path / "sweep.csv")], deps)
    assert code == 0
    assert "BEST CONFIG" in stdout
    assert "Top 5 configs" in stdout
    assert "Full results" in stdout


def test_sweep_command_without_best(tmp_path: Path) -> None:

    deps = EvalCliDeps(
        sweep_bm25=lambda **kw: SimpleNamespace(total_configs=0, total_duration_s=0.0, best=None, results=[]),
    )
    suite_in = tmp_path / "s.yaml"
    suite_in.write_text("cases:\n", encoding="utf-8")
    stdout, _stderr, code = _drive(["sweep", "--suite", str(suite_in)], deps)
    assert code == 0
    assert "BEST CONFIG" not in stdout


# ---------------------------------------------------------------------------
# main() defaults — KAIRIX_MONITOR_LOG env var fallback
# ---------------------------------------------------------------------------


def test_main_resolves_default_log_path_for_report(monkeypatch, tmp_path: Path) -> None:
    """When --log is omitted on report, the default Path.home()/.cache path is used."""

    captured: dict = {}

    def _gen(**kw: Any) -> str:
        captured.update(kw)
        return "body"

    # Patch Path.home (stdlib boundary) so the constructed default lives under tmp_path.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    _stdout, _stderr, code = _drive(["report"], EvalCliDeps(generate_report=_gen))
    assert code == 0
    assert captured.get("log_path", "").endswith("monitor.jsonl")
