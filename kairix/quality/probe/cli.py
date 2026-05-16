"""`kairix probe search` — CLI binding over `kairix.quality.probe.run_probe_search`.

Operator surface for the search-probe capability. The probe measures
concurrent-load latency to decide which Tier 1 tuning lever to pull
(see docs/architecture/teaming-concurrency-strategy.md).

The MCP surface (``tool_probe_search``) is hard-capped at queries<=20 and
concurrency<=3 so agents can't accidentally generate load runs. Agents
needing larger probes get an OperatorOnlyCapability envelope pointing
them back at this CLI.

Exit-code semantics (matches the rest of the kairix CLI):
    0 — passed (overall p95 within threshold, no errors)
    1 — gate failure (probe ran but threshold breached or errors observed)
    2 — indeterminate (invalid args, suite not found, etc.)
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import TYPE_CHECKING

from kairix.quality.probe.runner import (
    DEFAULT_P95_THRESHOLD_MS,
    run_probe_search,
)

if TYPE_CHECKING:
    from kairix.quality.probe.runner import ProbeResult


# Help affordance — name the MCP equivalent like soak/cli.py does so an
# operator reading --help learns which surface to use.
_HELP_DESCRIPTION = """\
Concurrent-load latency probe — sample a benchmark suite at the requested
concurrency and report p50/p95/p99 latency overall and per category. The
output decides which Tier 1 tuning lever to pull (Azure embed pool size,
query-result cache, connection pool).

MCP equivalent: tool_probe_search — hard-capped to queries<=20, concurrency<=3.
                Agents requesting larger probes receive an OperatorOnlyCapability
                envelope with the exact CLI command.

See: docs/architecture/teaming-concurrency-strategy.md
"""

# Affordance markers used in the FAIL path. Pulled to a constant so F17
# (no duplicated >=10-char string literal 3+ times) stays clean and the
# strings stay in lockstep across single + sweep modes.
_FIX_LINE = "fix: investigate the bottleneck (see recommendation below) and apply the Tier 1 lever"
_NEXT_LINE = "next: re-run with --recommend to see the suggested action; try --concurrency-sweep 1,2,5,10 to see where p95 first climbs"
_RUN_LINE = "run: kairix probe search --suite reflib --queries 100 --concurrency 5 --recommend"


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kairix probe search",
        description=_HELP_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # Reserved positional subcommand — leaves room for future probe
    # surfaces (e.g. `kairix probe embed`) without changing dispatch shape.
    p.add_argument(
        "subcommand",
        choices=["search"],
        help="probe subcommand (currently only 'search')",
    )
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
        action="store_true",
        help="append a one-line bottleneck recommendation after the run",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="emit JSON envelope on stdout; suppress human-readable output",
    )
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
        "next: see `kairix probe search --help` for the accepted ranges",
        file=sys.stderr,
    )
    return 2


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

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
