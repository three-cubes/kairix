"""`kairix probe (search|burst)` — CLI binding over the probe Python APIs.

Operator surface for the search-probe + burst-probe capabilities. Both
measure concurrent-load behaviour but answer different questions:

  - ``probe search`` — p50/p95/p99 latency at sustained concurrency. Decides
    which Tier 1 tuning lever to pull (see
    docs/architecture/teaming-concurrency-strategy.md).
  - ``probe burst`` — sustained-vs-peak QPS drop after warm-up. Catches
    post-warmup resource leaks and cache eviction that p95 averages over.

The MCP surfaces (``tool_probe_search``, ``tool_probe_burst``) have different
shapes: search is hard-capped (queries<=20, concurrency<=3) so agents can run
a healthcheck-shaped probe themselves; burst is escalation-only because it's
load-generating by design.

Exit-code semantics (matches the rest of the kairix CLI):
    0 — passed (gate target met, no errors)
    1 — gate failure (probe ran but threshold breached or errors observed)
    2 — indeterminate (invalid args, suite not found, etc.)
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import TYPE_CHECKING

from kairix.quality.probe.burst import (
    DEFAULT_QPS_DROP_PCT_THRESHOLD,
    run_probe_burst,
)
from kairix.quality.probe.runner import (
    DEFAULT_P95_THRESHOLD_MS,
    run_probe_search,
)

if TYPE_CHECKING:
    from kairix.quality.probe.burst import BurstResult
    from kairix.quality.probe.runner import ProbeResult


# Help affordance — name the MCP equivalent like soak/cli.py does so an
# operator reading --help learns which surface to use.
_HELP_DESCRIPTION = """\
Concurrent-load probes for the kairix retrieval pipeline.

  search — p50/p95/p99 latency at sustained concurrency; decides which
           Tier 1 tuning lever to pull (Azure embed pool, query-result
           cache, connection pool).
  burst  — sustained-vs-peak QPS drop after warm-up; catches post-warmup
           resource leaks and cache eviction that p95 averages over.

MCP equivalent: tool_probe_search — hard-capped to queries<=20, concurrency<=3.
                Agents requesting larger probes receive an OperatorOnlyCapability
                envelope with the exact CLI command.
                tool_probe_burst — operator-only escalation stub. Burst is
                load-generating by design; agents calling tool_probe_burst
                receive the OperatorOnlyCapability envelope with the exact
                CLI command.

See: docs/architecture/teaming-concurrency-strategy.md
"""

# Affordance markers used in the search FAIL path. Pulled to a constant so F17
# (no duplicated >=10-char string literal 3+ times) stays clean and the
# strings stay in lockstep across single + sweep modes.
_FIX_LINE = "fix: investigate the bottleneck (see recommendation below) and apply the Tier 1 lever"
_NEXT_LINE = "next: re-run with --recommend to see the suggested action; try --concurrency-sweep 1,2,5,10 to see where p95 first climbs"
_RUN_LINE = "run: kairix probe search --suite reflib --queries 100 --concurrency 5 --recommend"

# Burst FAIL-path affordance markers — kept distinct from search so an
# operator reading either output knows which subcommand to re-run.
_BURST_FIX_LINE = "fix: investigate sustained QPS degradation — likely cache eviction or resource leak under burst"
_BURST_NEXT_LINE = "next: re-run with --bucket-ms 250 to see finer-grained throughput trend; cross-check with kairix soak run --repeat 3"
_BURST_RUN_LINE = "run: kairix probe burst --suite reflib --total-queries 200 --peak-concurrency 20 --json | jq ."

# Argparse boolean-flag action — extracted because both search and burst
# subparsers use it for --json (search additionally uses it for --recommend),
# pushing the literal past the F17 ≥3-occurrence cap.
_STORE_TRUE = "store_true"


def _add_search_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--suite",
        required=True,
        help="benchmark suite name or path (e.g. 'reflib')",
    )
    p.add_argument(
        "--queries",
        type=int,
        default=100,
        help="number of sampled queries to run (>=1). Default 100.",
    )
    p.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="thread-pool size (>=1). Default 1.",
    )
    p.add_argument(
        "--seed",
        type=int,
        default=0,
        help="deterministic sample/shuffle seed. Default 0.",
    )
    p.add_argument(
        "--p95-threshold-ms",
        type=float,
        default=DEFAULT_P95_THRESHOLD_MS,
        help=f"pass-fail threshold for overall p95. Default {DEFAULT_P95_THRESHOLD_MS}.",
    )
    p.add_argument(
        "--concurrency-sweep",
        default=None,
        help=(
            "comma-separated list of concurrencies (e.g. '1,2,5,10,20'). "
            "When set, the probe runs once per value (same seed, same queries). "
            "Overrides --concurrency."
        ),
    )
    p.add_argument(
        "--recommend",
        action=_STORE_TRUE,
        help="append a one-line bottleneck recommendation after the run",
    )
    p.add_argument(
        "--json",
        action=_STORE_TRUE,
        help="emit JSON envelope on stdout; suppress human-readable output",
    )


def _add_burst_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--suite",
        required=True,
        help="benchmark suite name or path (e.g. 'reflib')",
    )
    p.add_argument(
        "--total-queries",
        type=int,
        default=200,
        help="total queries to inject as fast as possible (>=1). Default 200.",
    )
    p.add_argument(
        "--peak-concurrency",
        type=int,
        default=20,
        help="max worker count for the burst (>=1). Default 20.",
    )
    p.add_argument(
        "--bucket-ms",
        type=int,
        default=500,
        help="time-bucket width in milliseconds (>=1). Default 500.",
    )
    p.add_argument(
        "--seed",
        type=int,
        default=0,
        help="deterministic sample/shuffle seed. Default 0.",
    )
    p.add_argument(
        "--qps-drop-threshold-pct",
        type=float,
        default=DEFAULT_QPS_DROP_PCT_THRESHOLD,
        help=f"pass-fail cap on sustained-vs-peak QPS drop. Default {DEFAULT_QPS_DROP_PCT_THRESHOLD}.",
    )
    p.add_argument(
        "--include-warmup",
        action=_STORE_TRUE,
        help=(
            "include pre-completion (cold-start) and partial-final buckets in "
            "headline peak/sustained stats. Default off — auto-skip keeps the "
            "headline aligned with steady-state behaviour. Use to inspect the "
            "raw timeline."
        ),
    )
    p.add_argument(
        "--json",
        action=_STORE_TRUE,
        help="emit JSON envelope on stdout; suppress human-readable output",
    )


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kairix probe",
        description=_HELP_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="subcommand", required=True, metavar="subcommand")

    search_parser = sub.add_parser(
        "search",
        help="sustained-concurrency latency probe",
        description=_HELP_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_search_args(search_parser)

    burst_parser = sub.add_parser(
        "burst",
        help="burst-load throughput-drop probe",
        description=_HELP_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_burst_args(burst_parser)

    return p


def _parse_sweep(raw: str) -> list[int]:
    """Parse '--concurrency-sweep' value into a list of positive ints.

    Raises:
        ValueError: when any token is non-integer or <1.
    """
    out: list[int] = []
    for token in raw.split(","):
        cleaned = token.strip()
        if not cleaned:
            continue
        value = int(cleaned)  # ValueError on non-int — caught by caller
        if value < 1:
            raise ValueError(f"sweep value must be >= 1; got {value}")
        out.append(value)
    if not out:
        raise ValueError("sweep must contain at least one concurrency value")
    return out


def _format_overall(result: ProbeResult) -> str:
    o = result.overall
    return (
        f"  overall: p50={o.p50_ms}ms p95={o.p95_ms}ms p99={o.p99_ms}ms "
        f"n={o.n} mean_conc={result.mean_concurrency:.2f} "
        f"wallclock={result.wallclock_s:.2f}s errors={result.errors}"
    )


def _format_per_category(result: ProbeResult) -> list[str]:
    if not result.per_category:
        return []
    lines = ["  per_category:"]
    for cat in sorted(result.per_category.keys()):
        stats = result.per_category[cat]
        lines.append(f"    {cat}: p95={stats.p95_ms}ms (n={stats.n})")
    return lines


def _format_verdict(result: ProbeResult) -> list[str]:
    p95 = result.overall.p95_ms
    threshold = result.p95_threshold_ms
    if result.passed:
        return [f"PASS — p95 {p95}ms within {threshold}ms threshold"]
    reason = (
        f"FAIL — p95 {p95}ms exceeds {threshold}ms threshold at concurrency={result.concurrency}"
        if p95 > threshold
        else f"FAIL — {result.errors} error(s) during probe at concurrency={result.concurrency}"
    )
    return [reason, _FIX_LINE, _NEXT_LINE, _RUN_LINE]


def _format_recommendation(result: ProbeResult) -> list[str]:
    if result.bottleneck is None:
        return []
    kind, action = result.bottleneck
    return [f"recommendation: [{kind}] {action}"]


def _format_text(result: ProbeResult, *, recommend: bool) -> str:
    """Render a ProbeResult as the operator's human-readable report."""
    lines: list[str] = [
        f"probe: suite={result.suite} queries={result.queries} concurrency={result.concurrency} seed={result.seed}",
        _format_overall(result),
    ]
    lines.extend(_format_per_category(result))
    lines.extend(_format_verdict(result))
    if recommend:
        lines.extend(_format_recommendation(result))
    return "\n".join(lines)


def _run_single(args: argparse.Namespace, concurrency: int) -> ProbeResult:
    return run_probe_search(
        suite=args.suite,
        queries=args.queries,
        concurrency=concurrency,
        seed=args.seed,
        p95_threshold_ms=args.p95_threshold_ms,
    )


def _emit_single(args: argparse.Namespace) -> int:
    result = _run_single(args, args.concurrency)
    if args.json:
        print(json.dumps(result.to_envelope(), indent=2))
    else:
        print(_format_text(result, recommend=args.recommend))
    return 0 if result.passed else 1


def _emit_sweep(args: argparse.Namespace, sweep: list[int]) -> int:
    results = [_run_single(args, c) for c in sweep]
    if args.json:
        print(json.dumps({"runs": [r.to_envelope() for r in results]}, indent=2))
    else:
        blocks = [_format_text(r, recommend=args.recommend) for r in results]
        any_failed = any(not r.passed for r in results)
        summary = (
            "sweep: all runs passed"
            if not any_failed
            else f"sweep: {sum(1 for r in results if not r.passed)} of {len(results)} runs FAILED"
        )
        print("\n\n".join(blocks) + "\n\n" + summary)
    return 0 if all(r.passed for r in results) else 1


def _invalid_args(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    print("fix: correct the flag value and re-run", file=sys.stderr)
    print(
        "next: see `kairix probe --help` for the accepted ranges",
        file=sys.stderr,
    )
    return 2


def _format_burst_buckets(result: BurstResult) -> list[str]:
    skipped_idx = {s.index for s in result.skipped_buckets}
    lines = ["  buckets:"]
    for i, b in enumerate(result.buckets):
        marker = "  [skipped]" if i in skipped_idx else ""
        lines.append(
            f"    [{b.window_start_s}-{b.window_end_s}s) qps={b.qps} "
            f"completed={b.queries_completed}  errors={b.errors}{marker}"
        )
    return lines


def _format_burst_skip_summary(result: BurstResult) -> list[str]:
    if result.include_warmup:
        return ["  warmup-detection: disabled (raw timeline; --include-warmup)"]
    if not result.skipped_buckets:
        return ["  warmup-detection: clean (no buckets skipped)"]
    lines = ["  warmup-detection: auto-skip applied"]
    for s in result.skipped_buckets:
        lines.append(f"    skipped bucket {s.index}: {s.reason}")
    return lines


def _format_burst_verdict(result: BurstResult) -> list[str]:
    drop = result.qps_drop_pct
    threshold = result.qps_drop_threshold_pct
    if result.passed:
        return [f"PASS — qps_drop {drop}% within {threshold}% threshold"]
    reason = (
        f"FAIL — qps_drop {drop}% exceeds {threshold}% threshold "
        f"(peak={result.peak_qps} sustained={result.sustained_qps})"
        if drop > threshold
        else f"FAIL — {result.errors} error(s) during burst"
    )
    return [reason, _BURST_FIX_LINE, _BURST_NEXT_LINE, _BURST_RUN_LINE]


def _format_burst_text(result: BurstResult) -> str:
    """Render a BurstResult as the operator's human-readable report."""
    lines: list[str] = [
        f"probe burst: suite={result.suite} total_queries={result.total_queries} "
        f"peak_concurrency={result.peak_concurrency} bucket_ms={result.bucket_ms}",
        f"  peak_qps={result.peak_qps}  sustained_qps={result.sustained_qps}  "
        f"qps_drop={result.qps_drop_pct}%  wallclock={result.wallclock_s:.2f}s  errors={result.errors}",
    ]
    lines.extend(_format_burst_buckets(result))
    lines.extend(_format_burst_skip_summary(result))
    lines.extend(_format_burst_verdict(result))
    return "\n".join(lines)


def _emit_burst(args: argparse.Namespace) -> int:
    result = run_probe_burst(
        suite=args.suite,
        total_queries=args.total_queries,
        peak_concurrency=args.peak_concurrency,
        bucket_ms=args.bucket_ms,
        seed=args.seed,
        qps_drop_threshold_pct=args.qps_drop_threshold_pct,
        include_warmup=args.include_warmup,
    )
    if args.json:
        print(json.dumps(result.to_envelope(), indent=2))
    else:
        print(_format_burst_text(result))
    return 0 if result.passed else 1


def _run_search(args: argparse.Namespace) -> int:
    if args.queries < 1:
        return _invalid_args(f"--queries must be >= 1; got {args.queries}")
    if args.concurrency < 1:
        return _invalid_args(f"--concurrency must be >= 1; got {args.concurrency}")

    if args.concurrency_sweep is not None:
        try:
            sweep = _parse_sweep(args.concurrency_sweep)
        except ValueError as exc:
            return _invalid_args(f"--concurrency-sweep parse error: {exc}")
        return _emit_sweep(args, sweep)

    return _emit_single(args)


def _run_burst(args: argparse.Namespace) -> int:
    if args.total_queries < 1:
        return _invalid_args(f"--total-queries must be >= 1; got {args.total_queries}")
    if args.peak_concurrency < 1:
        return _invalid_args(f"--peak-concurrency must be >= 1; got {args.peak_concurrency}")
    if args.bucket_ms < 1:
        return _invalid_args(f"--bucket-ms must be >= 1; got {args.bucket_ms}")

    return _emit_burst(args)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.subcommand == "search":
        return _run_search(args)
    if args.subcommand == "burst":
        return _run_burst(args)
    # argparse's `required=True` on the subparser guarantees a known subcommand,
    # so this branch is unreachable in practice.
    return _invalid_args(f"unknown subcommand: {args.subcommand!r}")  # pragma: no cover — argparse required=True guards
