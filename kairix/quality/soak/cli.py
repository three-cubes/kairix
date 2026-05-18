"""`kairix soak run` — CLI binding over `kairix.quality.soak.run_soak`.

Operator surface for the soak capability. The MCP surface is a stub —
soak runs are minutes-long and load-generating; agents that hit
`tool_soak_run` get an OperatorOnlyCapability envelope that points them
back here.

Exit-code semantics (matches the rest of the kairix CLI):
    0 — passed
    1 — failures detected (gate failed for operator/CI)
    2 — indeterminate (workload couldn't run; bad arguments; etc.)
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import TYPE_CHECKING

from kairix.quality.soak.runner import (
    DEFAULT_MAX_LOG_VOLUME_MB_PER_REPEAT,
    DEFAULT_MAX_MEMORY_GROWTH_MB,
    DEFAULT_MAX_TIME_DRIFT_PCT,
    run_soak,
)

if TYPE_CHECKING:
    from kairix.quality.soak.runner import SoakResult


# Every CLI command's help block names its MCP equivalent (or the
# deliberate absence + escalation path) so an operator reading --help
# learns which surface to use.
_HELP_DESCRIPTION = """\
Operational soak test — repeat a workload and assert it holds together
across iterations. Catches "unit-fine, scale-fragile" regressions
(memory leak, log-spam, per-call factory rebuild, fd leak, state leak).

MCP equivalent: none — operator-only. Soak runs are multi-minute load
                runs; agents needing to verify load behaviour should call
                `tool_soak_run`, which returns an OperatorOnlyCapability
                envelope with the exact command for the operator to run.

See: docs/architecture/operational-tests-design.md
     docs/runbooks/kairix-retrieval-health.md (Soak section)
"""


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kairix soak run",
        description=_HELP_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # Reserved positional subcommand — leaves room for `status` / `baseline`
    # without changing the user-facing dispatch shape.
    p.add_argument(
        "subcommand",
        choices=["run"],
        help="soak subcommand (currently only 'run')",
    )
    p.add_argument(
        "--suite",
        required=True,
        help="benchmark suite name to repeat (e.g. 'reflib')",
    )
    p.add_argument(
        "--repeat",
        type=int,
        default=3,
        help="number of iterations (>=2 to compare). Default 3.",
    )
    p.add_argument(
        "--max-memory-growth-mb",
        type=float,
        default=DEFAULT_MAX_MEMORY_GROWTH_MB,
        help=f"per-iter RSS growth cap in MB. Default {DEFAULT_MAX_MEMORY_GROWTH_MB}.",
    )
    p.add_argument(
        "--max-log-volume-mb",
        type=float,
        default=DEFAULT_MAX_LOG_VOLUME_MB_PER_REPEAT,
        help=(
            "max stderr bytes (MB) per repeat. Total cap is value x repeat. "
            f"Default {DEFAULT_MAX_LOG_VOLUME_MB_PER_REPEAT}."
        ),
    )
    p.add_argument(
        "--max-time-drift-pct",
        type=float,
        default=DEFAULT_MAX_TIME_DRIFT_PCT,
        help=(
            "max %% drift in per-iter wall time vs iter-0. Skipped on sub-100ms baselines. "
            f"Default {DEFAULT_MAX_TIME_DRIFT_PCT}."
        ),
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="emit JSON envelope on stdout; suppress human-readable output",
    )
    return p


def _format_text(result: SoakResult) -> str:
    """Render a SoakResult as the human-readable operator report."""
    lines: list[str] = []
    lines.append(f"soak: suite={result.suite} repeat={result.repeat}")
    for it in result.iterations:
        lines.append(
            f"  iter {it.index}: duration={it.duration_s}s memory_delta={it.memory_mb}MB "
            f"stderr={it.stderr_bytes}B fd={it.fd_count} sig={it.signature}"
        )
    if result.error:
        lines.append("")
        lines.append(f"error: {result.error}")
        lines.append("fix: investigate the workload — it raised before all iterations completed")
        return "\n".join(lines)
    if result.passed:
        lines.append("")
        lines.append("PASS — no degradation detected across iterations")
        return "\n".join(lines)
    lines.append("")
    lines.append(f"FAIL — {len(result.failures)} assertion(s) failed:")
    for f in result.failures:
        loc = f"iter {f.iteration}: " if f.iteration is not None else ""
        lines.append(f"  - [{f.kind}] {loc}{f.detail}")
    lines.append("")
    lines.append("fix: investigate the assertion that fired (see kind= prefix)")
    lines.append("next: re-run with --repeat 2 to isolate; pass --json for the structured envelope")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.repeat < 2:
        print(
            "error: --repeat must be >= 2 to compare iterations",
            file=sys.stderr,
        )
        print("fix: pass --repeat 2 or higher", file=sys.stderr)
        return 2

    result = run_soak(
        suite=args.suite,
        repeat=args.repeat,
        max_memory_growth_mb=args.max_memory_growth_mb,
        max_log_volume_mb=args.max_log_volume_mb,
        max_time_drift_pct=args.max_time_drift_pct,
    )

    if args.json:
        print(json.dumps(result.to_envelope(), indent=2))
    else:
        print(_format_text(result))

    if result.error:
        return 2
    return 0 if result.passed else 1
