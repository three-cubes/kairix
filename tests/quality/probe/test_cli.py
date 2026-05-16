"""CLI binding tests for `kairix probe search`.

The Python API is tested directly in test_runner.py. These tests cover
the CLI shell: argument parsing, exit-code semantics, text vs JSON
output, sweep mode, the --recommend affordance, and that the FAIL path
emits the F21 actionable markers (fix:/next:/run:).

All tests drive through the public CLI surface — argparse + main(argv).
``run_probe_search`` is stubbed at its CLI-module binding site (not
monkey-patched on kairix internals) so the test exercises argument
plumbing only, never real search.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from typing import Any
from unittest import mock

import pytest

from kairix.quality.probe import cli as probe_cli
from kairix.quality.probe.runner import ProbeResult
from kairix.quality.probe.stats import LatencyStats

pytestmark = pytest.mark.unit


def _capture(argv: list[str]) -> tuple[int, str, str]:
    out_buf, err_buf = io.StringIO(), io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf):
        rc = probe_cli.main(argv)
    return rc, out_buf.getvalue(), err_buf.getvalue()


def _stats(p95: float = 100.0) -> LatencyStats:
    return LatencyStats(
        n=10,
        p50_ms=50.0,
        p95_ms=p95,
        p99_ms=p95 + 10.0,
        min_ms=10.0,
        max_ms=p95 + 50.0,
        mean_ms=60.0,
    )


def _pass_result(concurrency: int = 5) -> ProbeResult:
    return ProbeResult(
        suite="reflib",
        queries=100,
        concurrency=concurrency,
        seed=0,
        overall=_stats(p95=380.0),
        per_category={"recall": _stats(p95=350.0), "temporal": _stats(p95=370.0)},
        mean_concurrency=float(concurrency) * 0.95,
        wallclock_s=8.42,
        azure_429_count=0,
        errors=0,
        p95_threshold_ms=500.0,
        passed=True,
        bottleneck=None,
    )


def _fail_result(concurrency: int = 10) -> ProbeResult:
    return ProbeResult(
        suite="reflib",
        queries=100,
        concurrency=concurrency,
        seed=0,
        overall=_stats(p95=720.0),
        per_category={"recall": _stats(p95=750.0)},
        mean_concurrency=float(concurrency) * 0.95,
        wallclock_s=12.0,
        azure_429_count=0,
        errors=0,
        p95_threshold_ms=500.0,
        passed=False,
        bottleneck=(
            "pool_exhaustion_or_cache_miss",
            "p95=720.0ms over 500.0ms at concurrency=10 — likely pool exhaustion. Pull lever 1...",
        ),
    )


# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------


def test_help_exits_zero_and_names_mcp_equivalent() -> None:
    # Sabotage: drop --help handling or strip "MCP equivalent:" from
    # _HELP_DESCRIPTION and operators lose the cross-surface affordance.
    out_buf, err_buf = io.StringIO(), io.StringIO()
    with redirect_stdout(out_buf), redirect_stderr(err_buf), pytest.raises(SystemExit) as exc:
        probe_cli.main(["--help"])
    assert exc.value.code == 0
    stdout = out_buf.getvalue()
    assert stdout, "help should print to stdout"
    assert "MCP equivalent:" in stdout
    assert "tool_probe_search" in stdout


# ---------------------------------------------------------------------------
# Exit codes & single-run output
# ---------------------------------------------------------------------------


def test_passing_run_exits_zero_with_pass_marker() -> None:
    # Sabotage: invert the `passed`/exit-code mapping and exit 0 vs 1 flips.
    with mock.patch.object(probe_cli, "run_probe_search", return_value=_pass_result(concurrency=5)):
        rc, stdout, _stderr = _capture(["search", "--suite", "reflib", "--concurrency", "5"])
    assert rc == 0
    assert "PASS" in stdout
    assert "p95=380.0ms" in stdout
    assert "concurrency=5" in stdout


def test_failing_run_exits_one_with_f21_affordances() -> None:
    # Sabotage: remove the fix:/next:/run: lines from the FAIL formatter and
    # the gate output stops naming a corrective action, violating F21 affordance intent.
    with mock.patch.object(probe_cli, "run_probe_search", return_value=_fail_result(concurrency=10)):
        rc, stdout, _stderr = _capture(["search", "--suite", "reflib", "--concurrency", "10"])
    assert rc == 1
    assert "FAIL" in stdout
    assert "720.0ms" in stdout
    assert "500.0ms" in stdout
    assert "fix:" in stdout
    assert "next:" in stdout
    assert "run:" in stdout


# ---------------------------------------------------------------------------
# JSON envelope
# ---------------------------------------------------------------------------


def test_json_emits_valid_envelope_with_required_keys() -> None:
    # Sabotage: drop `to_envelope` from the JSON path (e.g. print str(result))
    # and json.loads raises / required keys disappear.
    with mock.patch.object(probe_cli, "run_probe_search", return_value=_pass_result(concurrency=2)):
        rc, stdout, _ = _capture(["search", "--suite", "reflib", "--concurrency", "2", "--json"])
    assert rc == 0
    payload = json.loads(stdout)
    assert isinstance(payload, dict)
    for key in ("suite", "queries", "concurrency", "overall", "passed", "bottleneck"):
        assert key in payload, f"envelope missing key {key!r}"
    assert payload["suite"] == "reflib"
    assert payload["passed"] is True


# ---------------------------------------------------------------------------
# Sweep mode
# ---------------------------------------------------------------------------


def test_sweep_runs_once_per_concurrency_same_seed() -> None:
    # Sabotage: collapse the sweep loop to a single call and the call_count drops
    # below 3 / seed forwarding diverges.
    seen_concurrencies: list[int] = []
    seen_seeds: set[int] = set()

    def fake_run(**kwargs: Any) -> ProbeResult:
        concurrency = int(kwargs["concurrency"])
        seen_concurrencies.append(concurrency)
        seen_seeds.add(int(kwargs["seed"]))
        return _pass_result(concurrency=concurrency)

    with mock.patch.object(probe_cli, "run_probe_search", side_effect=fake_run):
        rc, stdout, _ = _capture(
            ["search", "--suite", "reflib", "--concurrency-sweep", "1,2,5", "--seed", "42", "--json"]
        )
    assert rc == 0
    assert seen_concurrencies == [1, 2, 5]
    assert seen_seeds == {42}, "every sweep iteration must use the same seed"
    payload = json.loads(stdout)
    assert "runs" in payload
    assert len(payload["runs"]) == 3
    assert [r["concurrency"] for r in payload["runs"]] == [1, 2, 5]


# ---------------------------------------------------------------------------
# --recommend
# ---------------------------------------------------------------------------


def test_recommend_surfaces_bottleneck_action_on_failure() -> None:
    # Sabotage: drop `_format_recommendation` from the text formatter and the
    # recommended_action string disappears even when --recommend is set.
    with mock.patch.object(probe_cli, "run_probe_search", return_value=_fail_result(concurrency=10)):
        rc, stdout, _ = _capture(["search", "--suite", "reflib", "--concurrency", "10", "--recommend"])
    assert rc == 1
    assert "recommendation:" in stdout
    assert "pool_exhaustion_or_cache_miss" in stdout
    assert "likely pool exhaustion" in stdout


# ---------------------------------------------------------------------------
# Invalid arguments → exit 2
# ---------------------------------------------------------------------------


def test_queries_zero_exits_two_with_affordance() -> None:
    # Sabotage: remove the `args.queries < 1` guard and the CLI forwards to
    # run_probe_search, which raises — losing the structured exit-2 + affordance.
    rc, _stdout, stderr = _capture(["search", "--suite", "reflib", "--queries", "0"])
    assert rc == 2
    assert "--queries" in stderr
    assert "fix:" in stderr
    assert "next:" in stderr


def test_sweep_parse_error_exits_two() -> None:
    # Sabotage: drop the try/except around _parse_sweep and a non-int token
    # raises ValueError to the operator instead of an exit-2 + affordance.
    rc, _stdout, stderr = _capture(["search", "--suite", "reflib", "--concurrency-sweep", "abc,xyz"])
    assert rc == 2
    assert "--concurrency-sweep" in stderr
    assert "fix:" in stderr


def test_concurrency_zero_exits_two_with_affordance() -> None:
    # Sabotage: remove the `args.concurrency < 1` guard and the CLI forwards
    # to run_probe_search, losing the structured exit-2 + affordance markers.
    rc, _stdout, stderr = _capture(["search", "--suite", "reflib", "--concurrency", "0"])
    assert rc == 2
    assert "--concurrency" in stderr
    assert "fix:" in stderr


def test_sweep_value_below_one_exits_two() -> None:
    # Sabotage: drop the `value < 1` guard in _parse_sweep and `0` silently
    # forwards as a concurrency that the runner would then reject mid-flight
    # instead of failing fast with the CLI affordance.
    rc, _stdout, stderr = _capture(["search", "--suite", "reflib", "--concurrency-sweep", "0,2"])
    assert rc == 2
    assert "fix:" in stderr


def test_empty_sweep_exits_two() -> None:
    # Sabotage: drop the empty-list guard at the end of _parse_sweep and a
    # bare "," (no values) silently runs zero probes and exits 0, claiming a pass.
    rc, _stdout, stderr = _capture(["search", "--suite", "reflib", "--concurrency-sweep", ","])
    assert rc == 2
    assert "fix:" in stderr


# ---------------------------------------------------------------------------
# Text-mode sweep + per-category branches
# ---------------------------------------------------------------------------


def test_sweep_text_mode_lists_per_run_blocks_and_summary() -> None:
    # Sabotage: drop the per-block join / summary line in _emit_sweep and the
    # text sweep output collapses to a single block with no roll-up indicator.
    results = [_pass_result(concurrency=1), _pass_result(concurrency=2), _pass_result(concurrency=5)]

    def fake_run(**kwargs: Any) -> ProbeResult:
        c = int(kwargs["concurrency"])
        return next(r for r in results if r.concurrency == c)

    with mock.patch.object(probe_cli, "run_probe_search", side_effect=fake_run):
        rc, stdout, _ = _capture(["search", "--suite", "reflib", "--concurrency-sweep", "1,2,5"])
    assert rc == 0
    # All three concurrency tokens are surfaced in the header lines.
    assert "concurrency=1" in stdout
    assert "concurrency=2" in stdout
    assert "concurrency=5" in stdout
    assert "sweep: all runs passed" in stdout


def test_sweep_text_mode_fail_summary_counts_failures() -> None:
    # Sabotage: collapse the failed-runs count formula in _emit_sweep and the
    # summary stops naming how many runs failed.
    pass_run = _pass_result(concurrency=1)
    fail_run = _fail_result(concurrency=10)

    def fake_run(**kwargs: Any) -> ProbeResult:
        return pass_run if int(kwargs["concurrency"]) == 1 else fail_run

    with mock.patch.object(probe_cli, "run_probe_search", side_effect=fake_run):
        rc, stdout, _ = _capture(["search", "--suite", "reflib", "--concurrency-sweep", "1,10"])
    assert rc == 1
    assert "1 of 2 runs FAILED" in stdout


def test_passing_run_with_no_per_category_renders_cleanly() -> None:
    # Sabotage: invert the `if not result.per_category` guard in
    # _format_per_category and a probe with empty categories emits a stray
    # header line ("  per_category:") with nothing beneath it.
    empty_cat = ProbeResult(
        suite="reflib",
        queries=10,
        concurrency=1,
        seed=0,
        overall=_stats(p95=100.0),
        per_category={},
        mean_concurrency=0.95,
        wallclock_s=1.0,
        azure_429_count=0,
        errors=0,
        p95_threshold_ms=500.0,
        passed=True,
        bottleneck=None,
    )
    with mock.patch.object(probe_cli, "run_probe_search", return_value=empty_cat):
        rc, stdout, _ = _capture(["search", "--suite", "reflib"])
    assert rc == 0
    assert "per_category:" not in stdout


def test_recommend_with_no_bottleneck_is_silent() -> None:
    # Sabotage: drop the `bottleneck is None` early-return in
    # _format_recommendation and a healthy run with --recommend emits a
    # malformed "recommendation: [None] None" line instead of nothing.
    with mock.patch.object(probe_cli, "run_probe_search", return_value=_pass_result(concurrency=2)):
        rc, stdout, _ = _capture(["search", "--suite", "reflib", "--concurrency", "2", "--recommend"])
    assert rc == 0
    assert "recommendation:" not in stdout


# ---------------------------------------------------------------------------
# Wiring — top-level kairix dispatch lists 'probe'
# ---------------------------------------------------------------------------


def test_top_level_cli_dispatches_probe() -> None:
    # Sabotage: remove the 'probe' entry from COMMANDS and the top-level CLI
    # stops routing `kairix probe ...` to this module.
    from kairix.cli import COMMANDS

    assert "probe" in COMMANDS, "top-level CLI must dispatch 'probe' to the probe.cli module"
    module_path, fn_name, accepts_args = COMMANDS["probe"]
    assert module_path == "kairix.quality.probe.cli"
    assert fn_name == "main"
    assert accepts_args is True
