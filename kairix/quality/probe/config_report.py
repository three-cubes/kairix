"""JSON report shape for ``kairix probe-config`` (#provider-plugin-arch IM-9).

Mirrors the contract pinned in
``docs/architecture/probe-config-schema.md`` exactly — a stable,
provider-agnostic shape an end user can attach to a support issue.

The report intentionally surfaces hostnames only (never full URLs,
never credentials, never request/response bodies). Per F29 the probe
has no provider conditionals; this module's job is to fix the JSON
shape so every provider's report looks the same.

Public surface:

* ``ProbeConfigReport`` — dataclass that ``json.dumps`` produces
  the exact schema described in
  ``docs/architecture/probe-config-schema.md``.
* ``TuningRecommendation`` — entry shape inside
  ``tuning_recommendations``.
* ``Regression`` / ``Comparison`` — entries inside the optional
  ``comparison`` section populated when ``--compare baseline.json``
  was passed.
* ``EXIT_CODE_HEALTHY`` / ``EXIT_CODE_DEGRADED`` / ``EXIT_CODE_UNREACHABLE``
  — mirrored process-level exit codes (0 / 1 / 2) so shell scripts
  can branch without parsing JSON.
* ``SCHEMA_VERSION`` — the schema version string surfaced as
  ``schema_version`` on every report.
* ``compute_comparison(current, baseline)`` — diff helper that lists
  stages more than 20% slower than the baseline.
* ``hostname_from_endpoint(url_or_hostname)`` — privacy helper that
  strips path/query/auth fragments and returns the bare hostname.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

#: Schema version surfaced on every report. Bumped only on a
#: backward-incompatible field rename / removal. Adding new optional
#: fields keeps the same version.
SCHEMA_VERSION = "1.0"

#: Process exit code when ``status == "healthy"``. Mirrored from the
#: report's ``exit_code`` field so a shell script can branch without
#: parsing JSON.
EXIT_CODE_HEALTHY = 0

#: Process exit code when ``status == "degraded"``.
EXIT_CODE_DEGRADED = 1

#: Process exit code when ``status == "unreachable"``.
EXIT_CODE_UNREACHABLE = 2

#: Status strings used throughout the report and the CLI.
STATUS_HEALTHY = "healthy"
STATUS_DEGRADED = "degraded"
STATUS_UNREACHABLE = "unreachable"

#: A stage is reported as a regression only when ``current_ms`` is
#: more than this many percent slower than the baseline. Per
#: docs/architecture/probe-config-schema.md the 20% floor is "within
#: run-to-run noise" so anything below it is not flagged.
REGRESSION_THRESHOLD_PCT = 20.0


def _round2(value: float) -> float:
    """Round to 2 decimal places — keeps JSON output stable across runs.

    Used everywhere the report emits a float so the on-disk form is
    diffable and the comparison helper doesn't trip on floating-point
    noise below the round-off boundary.
    """
    return round(float(value), 2)


def hostname_from_endpoint(endpoint: str) -> str:
    """Return the bare hostname from a URL-or-hostname string.

    The probe-config report is intended to be shareable on a public
    issue tracker; per
    ``docs/architecture/probe-config-schema.md`` § Privacy we surface
    the hostname only — never the full URL with path / query / auth
    fragments.

    Accepts either form:

    - ``"https://example-resource.openai.azure.com/openai/v1"``
      → ``"example-resource.openai.azure.com"``
    - ``"api.openai.com"`` → ``"api.openai.com"``
    - ``"fake://provider"`` → ``"provider"`` (test fakes use this
      scheme; the surface still resolves to a hostname so the report
      shape stays uniform under tests).
    - ``""`` → ``""`` (degenerate but valid; an unreachable provider
      may have nothing to report).
    """
    if not endpoint:
        return ""
    parsed = urlparse(endpoint)
    if parsed.hostname:
        return parsed.hostname
    # No scheme: treat the whole string as a hostname-ish token.
    return endpoint.split("/", 1)[0]


@dataclass(frozen=True)
class TuningRecommendation:
    """Single row in ``tuning_recommendations``.

    Mirrors the schema's recommendation shape:

    - ``field`` — config field the operator should edit
      (``pool_size`` / ``coalesce_window_ms`` / ``cache_max_entries``).
    - ``current`` / ``suggested`` — current and proposed values
      shown verbatim to the operator.
    - ``rationale`` — one-sentence human-readable reason citing the
      observed metric that triggered the suggestion.
    """

    field: str
    current: Any
    suggested: Any
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "current": self.current,
            "suggested": self.suggested,
            "rationale": self.rationale,
        }


@dataclass(frozen=True)
class Regression:
    """One regressed stage inside the ``comparison.regressions`` list.

    A stage is added here only when its current latency is more than
    ``REGRESSION_THRESHOLD_PCT`` percent slower than the baseline.
    """

    stage: str
    baseline_ms: float
    current_ms: float
    percent_slower: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "baseline_ms": _round2(self.baseline_ms),
            "current_ms": _round2(self.current_ms),
            "percent_slower": _round2(self.percent_slower),
        }


@dataclass(frozen=True)
class Comparison:
    """The optional ``comparison`` section populated when ``--compare`` was passed.

    Lists which stages got slower than the baseline by more than
    ``REGRESSION_THRESHOLD_PCT`` percent. Stages within the noise
    floor are intentionally omitted — they are not actionable.
    """

    baseline_path: str
    baseline_collected_at: str
    regressions: list[Regression]

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_path": self.baseline_path,
            "baseline_collected_at": self.baseline_collected_at,
            "regressions": [r.to_dict() for r in self.regressions],
        }


@dataclass(frozen=True)
class ProviderInfo:
    """The ``provider`` section.

    Endpoint is intentionally a *hostname only* (see
    :func:`hostname_from_endpoint`) so the report is safe to share on
    a public issue tracker.
    """

    name: str
    endpoint_hostname: str
    dimension: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "endpoint_hostname": self.endpoint_hostname,
            "dimension": self.dimension,
        }


@dataclass(frozen=True)
class TimingSection:
    """End-to-end latency summary in milliseconds.

    All four values are rounded to 2 dp on emit so the JSON shape is
    diffable across runs.
    """

    cold_ms: float
    warm_p50_ms: float
    warm_p95_ms: float
    warm_p99_ms: float

    def to_dict(self) -> dict[str, float]:
        return {
            "cold_ms": _round2(self.cold_ms),
            "warm_p50_ms": _round2(self.warm_p50_ms),
            "warm_p95_ms": _round2(self.warm_p95_ms),
            "warm_p99_ms": _round2(self.warm_p99_ms),
        }


@dataclass(frozen=True)
class TransportSection:
    """Universal endpoint-concern metrics.

    Identical shape regardless of which provider is loaded — per F29
    the probe is singular and has no provider conditionals.
    """

    coalesce_ratio: float
    cache_hit_rate: float
    pool_acquire_p50_ms: float

    def to_dict(self) -> dict[str, float]:
        return {
            "coalesce_ratio": _round2(self.coalesce_ratio),
            "cache_hit_rate": _round2(self.cache_hit_rate),
            "pool_acquire_p50_ms": _round2(self.pool_acquire_p50_ms),
        }


@dataclass(frozen=True)
class ProbeConfigReport:
    """Full probe-config report — serialise via :meth:`to_dict` then
    ``json.dumps``.

    Field order is preserved by Python ≥3.7 dict-ordering so the
    on-disk JSON matches the schema document's reading order.
    """

    schema_version: str
    kairix_version: str
    status: str
    provider: ProviderInfo
    timing: TimingSection
    transport: TransportSection
    stage_latency_ms: dict[str, float]
    tuning_recommendations: list[TuningRecommendation]
    warnings: list[str] = field(default_factory=list)
    comparison: Comparison | None = None
    error: str | None = None
    exit_code: int = EXIT_CODE_HEALTHY

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schema_version": self.schema_version,
            "kairix_version": self.kairix_version,
            "status": self.status,
            "provider": self.provider.to_dict(),
            "timing": self.timing.to_dict(),
            "transport": self.transport.to_dict(),
            "stage_latency_ms": {k: _round2(v) for k, v in self.stage_latency_ms.items()},
            "tuning_recommendations": [r.to_dict() for r in self.tuning_recommendations],
            "warnings": list(self.warnings),
            "exit_code": self.exit_code,
        }
        if self.comparison is not None:
            out["comparison"] = self.comparison.to_dict()
        if self.error is not None:
            out["error"] = self.error
        return out


def compute_comparison(
    current_stages: dict[str, float],
    baseline_stages: dict[str, float],
    *,
    baseline_path: str,
    baseline_collected_at: str,
    threshold_pct: float = REGRESSION_THRESHOLD_PCT,
) -> Comparison:
    """Diff current vs baseline stage timings.

    A stage appears in the returned ``Comparison.regressions`` list
    only when ``current_ms`` is more than ``threshold_pct`` percent
    slower than ``baseline_ms``. Stages within ``threshold_pct`` are
    within run-to-run noise per
    ``docs/architecture/probe-config-schema.md`` and are intentionally
    omitted.

    Stages present in the baseline but missing from current are
    skipped (the provider may have been switched and the new one
    legitimately has no equivalent stage).
    """
    regressions: list[Regression] = []
    for stage, baseline_ms in baseline_stages.items():
        if stage not in current_stages:
            continue
        current_ms = current_stages[stage]
        if baseline_ms <= 0:
            continue
        percent_slower = ((current_ms - baseline_ms) / baseline_ms) * 100.0
        if percent_slower > threshold_pct:
            regressions.append(
                Regression(
                    stage=stage,
                    baseline_ms=baseline_ms,
                    current_ms=current_ms,
                    percent_slower=percent_slower,
                )
            )
    return Comparison(
        baseline_path=baseline_path,
        baseline_collected_at=baseline_collected_at,
        regressions=regressions,
    )


__all__ = [
    "EXIT_CODE_DEGRADED",
    "EXIT_CODE_HEALTHY",
    "EXIT_CODE_UNREACHABLE",
    "REGRESSION_THRESHOLD_PCT",
    "SCHEMA_VERSION",
    "STATUS_DEGRADED",
    "STATUS_HEALTHY",
    "STATUS_UNREACHABLE",
    "Comparison",
    "ProbeConfigReport",
    "ProviderInfo",
    "Regression",
    "TimingSection",
    "TransportSection",
    "TuningRecommendation",
    "compute_comparison",
    "hostname_from_endpoint",
]
