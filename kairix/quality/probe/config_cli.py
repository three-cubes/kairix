"""``kairix probe-config`` CLI wrapper (#provider-plugin-arch IM-9).

End-user-facing entry point that probes the configured provider and
emits a JSON health report plus tuning recommendations. Wired into
``kairix/cli.py``'s dispatch table; not a duplicated benchmark
harness — per F29 the probe instrumentation surface is singular and
this is the NEW CONSUMER of ``kairix.quality.probe.config_runner``.

The CLI shape:

* ``kairix probe-config`` — run against the provider named by
  ``KAIRIX_PROVIDER`` (or ``--provider <name>``); emit JSON to stdout.
* ``kairix probe-config --compare baseline.json`` — additionally
  populate the ``comparison`` section by diffing the current
  ``stage_latency_ms`` against the saved baseline. Stages >20%
  slower appear in ``comparison.regressions``.
* ``kairix probe-config --output report.json`` — write the JSON
  report to ``report.json`` instead of stdout. Useful for the
  "share this on your support issue" flow.

Exit codes (mirrored from the report's ``exit_code`` field per
``docs/architecture/probe-config-schema.md``):

* ``0`` — ``status == "healthy"``
* ``1`` — ``status == "degraded"``
* ``2`` — ``status == "unreachable"``
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from kairix.providers import (
    Provider,
    ProviderNotRegistered,
    ProviderRegistry,
    get_provider,
)
from kairix.quality.probe.config_report import (
    EXIT_CODE_UNREACHABLE,
    Comparison,
    ProbeConfigReport,
    compute_comparison,
)
from kairix.quality.probe.config_runner import (
    DEFAULT_CONCURRENCY,
    DEFAULT_REPEATED_SAMPLES,
    DEFAULT_WARM_SAMPLES,
    TransportSnapshotter,
    run_probe_config,
)

_HELP_DESCRIPTION = """\
kairix probe-config — probe the configured provider for health and tuning advice.

Run after configuring a provider (KAIRIX_PROVIDER env var or --provider flag)
to confirm the endpoint is reachable, capture cold/warm latencies, and get
tuning recommendations specific to your endpoint distance and latency tail.

The JSON report is provider-agnostic — same shape across azure_foundry,
openai, bedrock, ollama, anthropic, litellm_proxy.

Exit codes:
  0 — healthy
  1 — degraded (apply tuning_recommendations and re-run)
  2 — unreachable (check credentials / endpoint URL / firewall)

Privacy: endpoints surface as hostname only; no credentials or bodies are
emitted. The report is intended to be shareable on a public issue tracker.

See: docs/architecture/probe-config-schema.md
"""


def _build_parser() -> argparse.ArgumentParser:
    """Construct the argparse surface for ``kairix probe-config``."""
    p = argparse.ArgumentParser(
        prog="kairix probe-config",
        description=_HELP_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--provider",
        default=None,
        help=(
            "provider name to probe (overrides $KAIRIX_PROVIDER). "
            "Must match an installed plugin entry-point name "
            "(azure_foundry, openai, bedrock, ollama, anthropic, ...)."
        ),
    )
    p.add_argument(
        "--compare",
        default=None,
        metavar="BASELINE_JSON",
        help=(
            "path to a previous probe-config JSON report. When set, the "
            "current run's stage_latency_ms is diffed against the baseline "
            "and any stage >20%% slower is listed in comparison.regressions."
        ),
    )
    p.add_argument(
        "--output",
        default=None,
        metavar="REPORT_JSON",
        help=(
            "write the JSON report to this path instead of stdout. Useful when sharing the report on a support issue."
        ),
    )
    p.add_argument(
        "--warm-samples",
        type=int,
        default=DEFAULT_WARM_SAMPLES,
        help=f"warm sequential samples (>=1). Default {DEFAULT_WARM_SAMPLES}.",
    )
    p.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"fan-out for the warm-concurrent phase (>=1). Default {DEFAULT_CONCURRENCY}.",
    )
    p.add_argument(
        "--repeated-samples",
        type=int,
        default=DEFAULT_REPEATED_SAMPLES,
        help=f"repeated-query samples for the cache phase (>=1). Default {DEFAULT_REPEATED_SAMPLES}.",
    )
    return p


def _invalid_args(message: str) -> int:
    """Print an argparse-style error and return exit code 2.

    ``2`` matches the "indeterminate" exit code used by the rest of
    the kairix CLI; per the schema doc the same code is also used for
    ``unreachable`` so a shell script branching on exit-code alone
    treats invalid-args as "cannot run".

    fix: correct the flag value or env var; next: see
    ``kairix probe-config --help``; run: ``kairix probe-config --help``.
    """
    print(f"error: {message}", file=sys.stderr)
    print("fix: correct the flag value and re-run", file=sys.stderr)
    print("next: see `kairix probe-config --help` for the accepted ranges", file=sys.stderr)
    print("run: kairix probe-config --help", file=sys.stderr)
    return 2


def _default_env_provider_lookup() -> str | None:
    """Default env-var lookup — delegates to :func:`kairix.paths.provider_name`.

    Tests pass a callable that returns whatever they want; production
    falls through to the canonical paths-module reader so F4 stays
    clean (the env-var read happens at the kairix.paths boundary,
    not here).
    """
    from kairix.paths import provider_name

    return provider_name()


def _resolve_provider_name(
    args: argparse.Namespace,
    env_provider_lookup: Callable[[], str | None],
) -> str | None:
    """Pick the provider name from --provider then the env lookup.

    Returns ``None`` when neither was supplied — the CLI surfaces a
    clear actionable error in that case rather than picking an
    arbitrary default.
    """
    if args.provider:
        provider: str = args.provider
        return provider
    return env_provider_lookup()


def _resolve_provider(
    args: argparse.Namespace,
    registry: ProviderRegistry | None,
    env_provider_lookup: Callable[[], str | None],
) -> tuple[Provider | None, int]:
    """Resolve the configured provider name to a Provider instance.

    Returns ``(provider, 0)`` on success or ``(None, exit_code)`` on
    a resolution failure that should terminate the run.
    """
    name = _resolve_provider_name(args, env_provider_lookup)
    if not name:
        return None, _invalid_args("no provider configured. fix: pass --provider <name> or set KAIRIX_PROVIDER")
    try:
        provider = get_provider(name, registry=registry)
    except ProviderNotRegistered as exc:
        return None, _invalid_args(str(exc))
    return provider, 0


def _load_baseline(path_str: str) -> tuple[dict[str, Any] | None, int]:
    """Read a baseline JSON report from ``path_str``.

    Returns ``(report_dict, 0)`` on success or ``(None, exit_code)``
    when the file is missing / malformed. The CLI treats a bad
    baseline as an invalid-args failure (exit code 2) rather than
    silently skipping the comparison — operators expect the
    comparison to be in the report when they passed ``--compare``.
    """
    path = Path(path_str)
    if not path.exists():
        return None, _invalid_args(
            f"--compare path does not exist: {path_str}. fix: pass a previously-saved probe-config JSON report"
        )
    try:
        loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, _invalid_args(
            f"--compare path is not valid JSON ({path_str}): {exc}. "
            f"fix: pass a probe-config JSON report saved with --output"
        )
    return loaded, 0


def _attach_comparison(
    report: ProbeConfigReport,
    baseline: dict[str, Any],
    baseline_path: str,
) -> ProbeConfigReport:
    """Build a new report with the ``comparison`` section populated.

    Reports are frozen dataclasses; this returns a new instance with
    the ``comparison`` field set rather than mutating in place.
    """
    baseline_stages_raw = baseline.get("stage_latency_ms", {})
    if not isinstance(baseline_stages_raw, dict):
        baseline_stages = {}
    else:
        baseline_stages = {k: float(v) for k, v in baseline_stages_raw.items()}
    baseline_collected_at = str(baseline.get("collected_at", ""))
    comparison: Comparison = compute_comparison(
        current_stages=report.stage_latency_ms,
        baseline_stages=baseline_stages,
        baseline_path=baseline_path,
        baseline_collected_at=baseline_collected_at,
    )
    # frozen dataclass — construct a copy with the comparison field set.
    return ProbeConfigReport(
        schema_version=report.schema_version,
        kairix_version=report.kairix_version,
        status=report.status,
        provider=report.provider,
        timing=report.timing,
        transport=report.transport,
        stage_latency_ms=report.stage_latency_ms,
        tuning_recommendations=report.tuning_recommendations,
        warnings=report.warnings,
        comparison=comparison,
        error=report.error,
        exit_code=report.exit_code,
    )


def _emit_report(report: ProbeConfigReport, output_path: str | None) -> None:
    """Serialise ``report`` to JSON and write to stdout or ``output_path``."""
    payload = json.dumps(report.to_dict(), indent=2, sort_keys=False)
    if output_path:
        Path(output_path).write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)


def main(
    argv: list[str] | None = None,
    *,
    registry: ProviderRegistry | None = None,
    snapshotter: TransportSnapshotter | None = None,
    env_provider_lookup: Callable[[], str | None] | None = None,
) -> int:
    """Entry point invoked from ``kairix/cli.py``.

    Parameters:

    - ``argv`` — argv slice after the ``probe-config`` token; ``None``
      means ``sys.argv[1:]`` per argparse default.
    - ``registry`` — :class:`ProviderRegistry` injection for tests;
      production passes ``None`` and gets the default
      ``EntryPointRegistry``.
    - ``snapshotter`` — :class:`TransportSnapshotter` injection for
      tests; production passes ``None`` and gets the runner's default
      ``NullTransportSnapshotter`` (transport stats reported as zero
      until a wired-up snapshotter ships in a follow-up).
    - ``env_provider_lookup`` — env-var lookup callable returning the
      configured provider name or ``None``. Defaults to
      :func:`kairix.paths.provider_name` (canonical F4-clean reader).
      Tests pass a fixed-value lookup so they don't have to mutate the
      real process env.
    """
    if env_provider_lookup is None:
        env_provider_lookup = _default_env_provider_lookup
    args = _build_parser().parse_args(argv)
    if args.warm_samples < 1:
        return _invalid_args(f"--warm-samples must be >= 1; got {args.warm_samples}")
    if args.concurrency < 1:
        return _invalid_args(f"--concurrency must be >= 1; got {args.concurrency}")
    if args.repeated_samples < 1:
        return _invalid_args(f"--repeated-samples must be >= 1; got {args.repeated_samples}")

    provider, err = _resolve_provider(args, registry, env_provider_lookup)
    if provider is None:
        return err

    report = run_probe_config(
        provider,
        warm_samples=args.warm_samples,
        concurrency=args.concurrency,
        repeated_samples=args.repeated_samples,
        snapshotter=snapshotter,
    )

    if args.compare:
        baseline, err = _load_baseline(args.compare)
        if baseline is None:
            # The probe ran fine but the baseline file is broken; emit
            # whatever report we have so the operator can still see
            # the health verdict, then return the invalid-args code.
            _emit_report(report, args.output)
            return err
        report = _attach_comparison(report, baseline, args.compare)

    _emit_report(report, args.output)

    # Defensive: if the runner produced an unreachable report despite
    # not raising, mirror exit code 2 so shell scripts can branch.
    if report.exit_code == EXIT_CODE_UNREACHABLE:
        return EXIT_CODE_UNREACHABLE
    return report.exit_code


__all__ = ["main"]
