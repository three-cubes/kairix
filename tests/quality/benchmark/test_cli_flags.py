"""Sabotage-proven unit tests for the unified ``kairix benchmark run`` flags.

P5 of the unified benchmark initiative — covers the new flags introduced
in the canonical CLI:

* ``--mode {legacy,single-shot,concurrent,soak}`` — concurrent + soak
  emit the F21-formatted stub affordance and exit 1.
* ``--categories`` — filters the suite's case list before scoring.
* ``--metrics`` — echoed on the report header (opt-in scorer selection).
* ``--scope`` — echoed on the report header (per-run routing override).
* ``--gates`` — exits 2 on gate failure; informational without the flag.
* ``--baseline`` — emits a compare-with-previous line alongside the
  headline.

Each test follows the F1-clean / F2-clean / F5-clean pattern from the
existing ``tests/benchmark/test_cli.py``: collaborators flow through
``BenchmarkCLIDeps``; no ``@patch``, no env-var monkeypatching, no
internal-name imports.

Sabotage proofs are executed (mutate prod → confirm fail → restore)
during development and noted in the docstring of each test so the
review checklist can spot-check one.
"""

from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

import pytest

from kairix.quality.benchmark.cli import (
    BenchmarkCLIDeps,
    cmd_run,
    main,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _CapturingRunner:
    """Fake ``run_benchmark`` capturing kwargs and returning a stub result.

    Same shape as the existing ``tests/benchmark/test_cli.py`` fake — kept
    local to this file so the two test surfaces stay independent.
    """

    def __init__(self, *, gates_pass: bool = True, weighted_total: float = 0.5) -> None:
        self.calls: list[dict[str, Any]] = []
        self._gates_pass = gates_pass
        self._weighted_total = weighted_total

    def __call__(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        from kairix.quality.benchmark.runner import BenchmarkResult

        return BenchmarkResult(
            meta={
                "system": "fake",
                "agent": None,
                "date": "2026-05-21",
                "collection": kwargs.get("collection"),
            },
            summary={
                "weighted_total": self._weighted_total,
                "category_scores": {},
                "gates": {"phase1": self._gates_pass, "phase2": self._gates_pass},
            },
            diagnostics={},
            cases=[],
        )


@pytest.fixture
def bundled_suites(tmp_path: Path) -> Path:
    """Minimal bundled-suites directory with a multi-category suite.

    Multiple categories in the YAML so ``--categories`` has something to
    filter. Tests pass the explicit path so the loader never reads from
    ``KAIRIX_SUITES_ROOT`` (F2-clean).
    """
    suites = tmp_path / "suites"
    suites.mkdir()
    (suites / "unified.yaml").write_text(
        "meta:\n"
        "  name: unified\n"
        "  description: multi-category suite for CLI flag tests\n"
        "  default_collection: ref-library\n"
        "cases:\n"
        "  - id: R01\n"
        "    category: recall\n"
        "    query: recall query\n"
        "    score_method: exact\n"
        "    gold_title: alpha\n"
        "  - id: E01\n"
        "    category: entity\n"
        "    query: entity query\n"
        "    score_method: exact\n"
        "    gold_title: beta\n"
        "  - id: T01\n"
        "    category: temporal\n"
        "    query: temporal query\n"
        "    score_method: exact\n"
        "    gold_title: gamma\n",
    )
    return suites


def _ns(suite: str, **overrides: Any) -> argparse.Namespace:
    """Build an argparse.Namespace with the unified-flag defaults applied.

    Mirrors what argparse would produce so cmd_run sees the full flag
    surface — including flags the test isn't exercising — without us
    re-parsing argv per test.
    """
    base = {
        "subcommand": "run",
        "suite": suite,
        "system": "hybrid",
        "agent": None,
        "scope": None,
        "collection": None,
        "categories": None,
        "metrics": None,
        "gates": False,
        "baseline": None,
        "fusion": None,
        "output": None,
        "mode": "legacy",
    }
    base.update(overrides)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------------------
# --mode {concurrent, soak} — stub affordance
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cmd_run_mode_concurrent_emits_stub_affordance(bundled_suites: Path) -> None:
    """--mode concurrent emits the F21-formatted affordance and exits 1.

    sabotage: drop the ``mode_arg in (...)`` guard in cmd_run — the
    runner sees mode="concurrent" and the assertion below fails because
    the runner was invoked (calls is non-empty).
    """
    runner = _CapturingRunner()
    args = _ns(str(bundled_suites / "unified.yaml"), mode="concurrent")

    err = io.StringIO()
    with redirect_stderr(err), redirect_stdout(io.StringIO()):
        rc = cmd_run(args, deps=BenchmarkCLIDeps(run_benchmark=runner))

    assert rc == 1, f"expected exit 1 for stub mode; got {rc}"
    assert runner.calls == [], "runner must not be invoked for stub modes"
    text = err.getvalue()
    assert "--mode concurrent" in text
    assert "fix:" in text and "next:" in text and "run:" in text
    assert "kairix.quality.probe.runner.run_probe_search" in text


@pytest.mark.unit
def test_cmd_run_mode_soak_emits_stub_affordance(bundled_suites: Path) -> None:
    """--mode soak emits the F21-formatted affordance and exits 1.

    sabotage: change the api-hint selector to point at the concurrent
    helper unconditionally — the assertion below trips because the
    affordance points at the wrong Python API.
    """
    runner = _CapturingRunner()
    args = _ns(str(bundled_suites / "unified.yaml"), mode="soak")

    err = io.StringIO()
    with redirect_stderr(err), redirect_stdout(io.StringIO()):
        rc = cmd_run(args, deps=BenchmarkCLIDeps(run_benchmark=runner))

    assert rc == 1
    text = err.getvalue()
    assert "--mode soak" in text
    assert "kairix.quality.soak.run_soak" in text


# ---------------------------------------------------------------------------
# --categories — filter the case list before scoring
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cmd_run_categories_filters_to_subset(bundled_suites: Path) -> None:
    """--categories filters to the requested subset before scoring.

    sabotage: drop the ``_apply_categories(suite, args)`` call in cmd_run —
    the runner sees all 3 cases instead of just the recall + entity pair
    and the assertion below fails.
    """
    runner = _CapturingRunner()
    args = _ns(str(bundled_suites / "unified.yaml"), categories="recall,entity")

    out = io.StringIO()
    with redirect_stdout(out):
        rc = cmd_run(args, deps=BenchmarkCLIDeps(run_benchmark=runner))

    assert rc == 0
    suite = runner.calls[0]["suite"]
    case_categories = sorted(c.category for c in suite.cases)
    assert case_categories == ["entity", "recall"], f"unexpected case set: {case_categories}"
    assert "filtering to categories" in out.getvalue()


@pytest.mark.unit
def test_cmd_run_categories_empty_string_no_filter(bundled_suites: Path) -> None:
    """An empty --categories value preserves every case (no-op filter).

    sabotage: change ``if not raw`` to ``if raw is None`` in _parse_csv —
    an empty string slips through and the case set narrows to zero, which
    the assertion below catches.
    """
    runner = _CapturingRunner()
    args = _ns(str(bundled_suites / "unified.yaml"), categories="")

    cmd_run(args, deps=BenchmarkCLIDeps(run_benchmark=runner))

    suite = runner.calls[0]["suite"]
    assert len(suite.cases) == 3, "empty --categories must keep all cases"


@pytest.mark.unit
def test_cmd_run_categories_unknown_drops_all(bundled_suites: Path) -> None:
    """An unknown category filters every case out (zero scored).

    sabotage: change the comprehension to ``c.category not in wanted`` —
    the filter inverts and every case is kept, which the assertion below
    catches.
    """
    runner = _CapturingRunner()
    args = _ns(str(bundled_suites / "unified.yaml"), categories="does-not-exist")

    cmd_run(args, deps=BenchmarkCLIDeps(run_benchmark=runner))

    suite = runner.calls[0]["suite"]
    assert len(suite.cases) == 0, "unknown category must filter all cases out"


# ---------------------------------------------------------------------------
# --metrics — echo on the report header
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cmd_run_metrics_echoed_on_header(bundled_suites: Path) -> None:
    """--metrics is echoed on stdout so the operator sees their opt-in.

    sabotage: drop the metrics echo line from _emit_run_header — the
    assertion below trips because the output is missing the marker.
    """
    runner = _CapturingRunner()
    args = _ns(str(bundled_suites / "unified.yaml"), metrics="ndcg,judge,latency")

    out = io.StringIO()
    with redirect_stdout(out):
        cmd_run(args, deps=BenchmarkCLIDeps(run_benchmark=runner))

    text = out.getvalue()
    assert "metrics:" in text
    assert "'ndcg'" in text and "'judge'" in text and "'latency'" in text


@pytest.mark.unit
def test_cmd_run_metrics_default_no_echo(bundled_suites: Path) -> None:
    """When --metrics is omitted the metrics echo line stays silent.

    sabotage: emit the metrics line unconditionally — the assertion below
    trips because the output now contains the marker on a default run.
    """
    runner = _CapturingRunner()
    args = _ns(str(bundled_suites / "unified.yaml"))

    out = io.StringIO()
    with redirect_stdout(out):
        cmd_run(args, deps=BenchmarkCLIDeps(run_benchmark=runner))

    assert "metrics:" not in out.getvalue()


# ---------------------------------------------------------------------------
# --scope — echo on the report header
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cmd_run_scope_echoed_on_header(bundled_suites: Path) -> None:
    """--scope is echoed so the operator sees the per-run override.

    sabotage: rename the echo line to omit ``scope override`` — the
    assertion below trips because the marker is missing.
    """
    runner = _CapturingRunner()
    args = _ns(str(bundled_suites / "unified.yaml"), scope="agent")

    out = io.StringIO()
    with redirect_stdout(out):
        cmd_run(args, deps=BenchmarkCLIDeps(run_benchmark=runner))

    assert "scope override:" in out.getvalue()
    assert "'agent'" in out.getvalue()


# ---------------------------------------------------------------------------
# --gates — exit 2 on gate failure
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cmd_run_gates_flag_exits_two_on_failure(bundled_suites: Path) -> None:
    """--gates exits 2 when any declared gate fails.

    sabotage: change ``return 2`` in _emit_gate_failure to ``return 0`` —
    the assertion below trips because the exit code now masks the failure.
    """
    runner = _CapturingRunner(gates_pass=False)
    args = _ns(str(bundled_suites / "unified.yaml"), gates=True)

    err = io.StringIO()
    with redirect_stderr(err), redirect_stdout(io.StringIO()):
        rc = cmd_run(args, deps=BenchmarkCLIDeps(run_benchmark=runner))

    assert rc == 2, f"expected exit 2 on gate failure with --gates; got {rc}"
    text = err.getvalue()
    assert "gate failure" in text
    assert "fix:" in text and "next:" in text and "run:" in text


@pytest.mark.unit
def test_cmd_run_no_gates_flag_exits_zero_on_failure(bundled_suites: Path) -> None:
    """Without --gates, a gate failure stays informational (exit 0).

    sabotage: drop the ``getattr(args, 'gates', False)`` guard — the
    assertion below trips because the runner now exits 2 even when
    --gates is not passed.
    """
    runner = _CapturingRunner(gates_pass=False)
    args = _ns(str(bundled_suites / "unified.yaml"), gates=False)

    with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
        rc = cmd_run(args, deps=BenchmarkCLIDeps(run_benchmark=runner))

    assert rc == 0, f"expected exit 0 without --gates even on failure; got {rc}"


@pytest.mark.unit
def test_cmd_run_gates_flag_exits_zero_on_pass(bundled_suites: Path) -> None:
    """--gates exits 0 when every gate passes.

    sabotage: invert ``_gates_passed`` to ``return False`` — the assertion
    below trips because every clean run now exits 2.
    """
    runner = _CapturingRunner(gates_pass=True)
    args = _ns(str(bundled_suites / "unified.yaml"), gates=True)

    with redirect_stdout(io.StringIO()):
        rc = cmd_run(args, deps=BenchmarkCLIDeps(run_benchmark=runner))

    assert rc == 0


# ---------------------------------------------------------------------------
# --baseline — compare-with-previous summary
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cmd_run_baseline_emits_compare_line(bundled_suites: Path, tmp_path: Path) -> None:
    """--baseline emits a compare-with-previous summary alongside headline.

    sabotage: drop the ``_emit_baseline_compare(...)`` call in cmd_run —
    the assertion below trips because the marker is missing from stdout.
    """
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"summary": {"weighted_total": 0.4}}))
    runner = _CapturingRunner(weighted_total=0.6)
    args = _ns(str(bundled_suites / "unified.yaml"), baseline=str(baseline))

    out = io.StringIO()
    with redirect_stdout(out):
        cmd_run(args, deps=BenchmarkCLIDeps(run_benchmark=runner))

    text = out.getvalue()
    assert "baseline compare:" in text
    assert "0.400" in text and "0.600" in text
    assert "▲" in text, "delta marker (up) must appear on a positive delta"


@pytest.mark.unit
def test_cmd_run_baseline_missing_file_skips_gracefully(bundled_suites: Path, tmp_path: Path) -> None:
    """A missing baseline file emits a skip notice but doesn't abort the run.

    sabotage: re-raise the FileNotFoundError in _emit_baseline_compare —
    the cmd_run exits with an exception and the rc != 0 assertion trips.
    """
    runner = _CapturingRunner(weighted_total=0.6)
    args = _ns(
        str(bundled_suites / "unified.yaml"),
        baseline=str(tmp_path / "missing.json"),
    )

    out = io.StringIO()
    with redirect_stdout(out):
        rc = cmd_run(args, deps=BenchmarkCLIDeps(run_benchmark=runner))

    assert rc == 0, "missing baseline must not abort the run"
    assert "baseline compare skipped:" in out.getvalue()


# ---------------------------------------------------------------------------
# main() argparse integration — verify the new flags parse cleanly
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_main_run_accepts_new_flags(bundled_suites: Path) -> None:
    """The new flags survive argparse and reach cmd_run with the right shape.

    sabotage: remove ``--gates`` from the run subparser — argparse raises
    SystemExit on the unknown flag and the assertion below trips.
    """
    runner = _CapturingRunner()
    out = io.StringIO()
    with redirect_stdout(out):
        rc = main(
            [
                "run",
                "--suite",
                str(bundled_suites / "unified.yaml"),
                "--scope",
                "agent",
                "--categories",
                "recall",
                "--metrics",
                "ndcg,latency",
                "--gates",
                "--baseline",
                "/tmp/no-such-file.json",
                "--mode",
                "single-shot",
            ],
            deps=BenchmarkCLIDeps(run_benchmark=runner),
        )

    assert rc == 0
    assert len(runner.calls) == 1
    # The legacy --mode "legacy" maps to None; everything else passes through.
    assert runner.calls[0]["mode"] == "single-shot"
    # --categories=recall narrows the suite to one case.
    assert len(runner.calls[0]["suite"].cases) == 1


# ---------------------------------------------------------------------------
# Surface-disambiguation hint on `kairix eval` CLI.
# Conversation-eval (the legacy surface, still canonical for its shape)
# and gold-suite benchmark (`kairix benchmark run`) are complementary,
# not interchangeable — the hint helps operators reaching for the wrong
# one find the right one.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_kairix_eval_emits_surface_hint() -> None:
    """``kairix eval`` writes a one-line surface hint to stderr at entry.

    sabotage: drop the ``_emit_surface_hint(err_sink)`` call in
    kairix.use_cases.eval_suite.main — the assertion below trips because
    stderr is empty.
    """
    from kairix.use_cases.eval_suite import main as eval_main

    err = io.StringIO()
    # An invalid argv triggers argparse → SystemExit; we don't care about
    # the dispatch outcome, only that the hint fires BEFORE the exit.
    with pytest.raises(SystemExit), redirect_stderr(io.StringIO()):
        eval_main([], err=err)

    text = err.getvalue()
    assert "hint:" in text, f"expected surface hint on stderr; got: {text!r}"
    assert "kairix benchmark run" in text
    assert "conversation-eval" in text
    assert "gold-suite" in text


@pytest.mark.unit
def test_cmd_run_gates_flag_fails_on_incomplete_judge_coverage(tmp_path: Path) -> None:
    """Every judged case scores 1.0 (all phase gates pass) but one judge call
    fails: ``--gates`` exits 3 (inconclusive, the shared completeness exit)
    with a PARTIAL diagnostic. The same suite fully judged exits 0.

    Drives the real ``run_benchmark`` through the CLI's ``run_benchmark``
    seam with a FakeChatBackend + fake retrieve (no provider, no corpus).

    sabotage: drop the ``judge_failures(result)`` check in ``cmd_run`` — the
    partial run falls through to the gates and exits 2 (or 0 without the
    judge_coverage gate) instead of 3. Restored.
    """
    from kairix.quality.benchmark.runner import BenchmarkDeps, run_benchmark
    from tests.fakes import FakeChatBackend

    categories = ("recall", "temporal", "entity", "conceptual", "multi_hop", "procedural", "recall")
    suite = tmp_path / "judged.yaml"
    suite.write_text(
        "meta:\n  name: judged\n  version: '1.0'\ncases:\n"
        + "".join(
            f"  - id: C{i}\n    category: {cat}\n    query: q{i}\n    score_method: llm\n    gold_title: g{i}\n"
            for i, cat in enumerate(categories)
        ),
    )

    def _retrieve(**_kw: Any) -> tuple[list[str], list[str], dict[str, Any]]:
        return ["vault/doc.md"], ["snippet"], {}

    def _runner_with(n_replies: int) -> Any:
        def _run(**kwargs: Any) -> Any:
            backend = FakeChatBackend(responses=["1.0"] * n_replies)
            return run_benchmark(**kwargs, deps=BenchmarkDeps(chat_backend=backend, retrieve=_retrieve))

        return _run

    for n_replies, expected_rc in ((len(categories) - 1, 3), (len(categories), 0)):
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(out):
            rc = cmd_run(_ns(str(suite), gates=True), deps=BenchmarkCLIDeps(run_benchmark=_runner_with(n_replies)))
        assert rc == expected_rc, f"{n_replies} judge replies: expected exit {expected_rc}, got {rc}"
        if expected_rc == 3:
            assert "JUDGE COVERAGE gate (0 judge failures): FAIL" in out.getvalue()
            assert "PARTIAL" in err.getvalue()


@pytest.mark.unit
def test_cmd_run_baseline_compare_warns_when_baseline_is_partial(bundled_suites: Path, tmp_path: Path) -> None:
    """The informational ``--baseline`` header prints a PARTIAL warning when
    the stored baseline had judge failures (display-only: exit stays 0).

    sabotage: drop the partial-warning loop in ``_emit_baseline_compare`` —
    no PARTIAL line is printed. Restored.
    """
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"summary": {"weighted_total": 0.9, "judge_failures": 2}}), encoding="utf-8")
    args = _ns(str(bundled_suites / "unified.yaml"), baseline=str(baseline))
    out = io.StringIO()
    with redirect_stderr(io.StringIO()), redirect_stdout(out):
        rc = cmd_run(args, deps=BenchmarkCLIDeps(run_benchmark=_CapturingRunner()))
    assert rc == 0
    assert "PARTIAL RESULT: baseline" in out.getvalue()


@pytest.mark.unit
def test_cmd_run_partial_result_is_informational_without_gates(bundled_suites: Path) -> None:
    """Without ``--gates`` (here: no ``gates`` attribute at all, as from an
    older caller's Namespace) a partial run is informational: exit 0.

    sabotage: change ``getattr(args, "gates", False)`` to default ``True`` —
    the partial run exits 3 without the flag. Restored.
    """

    class _PartialRunner(_CapturingRunner):
        def __call__(self, **kwargs: Any) -> Any:
            result = super().__call__(**kwargs)
            result.summary["judge_failures"] = 1
            return result

    args = _ns(str(bundled_suites / "unified.yaml"))
    del args.gates
    with redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
        rc = cmd_run(args, deps=BenchmarkCLIDeps(run_benchmark=_PartialRunner()))
    assert rc == 0
