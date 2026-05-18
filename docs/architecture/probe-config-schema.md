# `kairix probe-config` JSON report schema

> **Status**: proposed (Wave 1 SK-7; instrumentation lands in Wave 2 IM-9).
> Companion to [`provider-plugin-architecture.md`](provider-plugin-architecture.md).
> Defines the JSON shape that an end user gets from
> `kairix probe-config` after they've configured their provider — a
> small representative workload run against their own endpoint, emitting
> a health report plus tuning advice tailored to their latency tail.

## Why this document exists

The probe instrumentation in `kairix/quality/probe/` is **singular** —
one measurement surface used by two consumers: the PVT release gate
and the end-user `kairix probe-config` health check. This document
fixes the **report contract** for the end-user consumer so its shape
is stable across providers and across releases. It is the schema an
operator's tooling (or a support engineer reading a shared report) can
rely on.

The schema MUST NOT contain provider-specific fields. Per F29 the probe
has no provider conditionals; per the ADR the report shape is identical
across `azure_foundry`, `openai`, `bedrock`, `ollama`, `anthropic`, and
`litellm_proxy`. Per-provider behavioural differences surface only as
stage-timing variation, which is provider-agnostic by contract.

## When to run `kairix probe-config`

- **After initial setup** — confirm the provider is reachable and that
  warm-path latencies are within the green envelope.
- **After changing provider config** — when switching from
  `azure_foundry` to `openai` (or model, or region), re-run to capture
  a new baseline.
- **Before opening a support issue** — attach the JSON report to the
  issue so the maintainers see your endpoint's actual latency tail and
  coalesce/cache behaviour, not a description of it.
- **Periodically (weekly)** — diff against a saved baseline with
  `--compare baseline.json` to catch silent provider-side regressions.

## What to do with the output

- `status: healthy` and empty `tuning_recommendations` — nothing to do.
- `status: degraded` — apply each recommendation in
  `tuning_recommendations` (each entry tells you which config field to
  edit, the current value, the suggested value, and why). Re-run to
  confirm.
- `status: unreachable` — check the `error` field; common causes are
  bad credentials, wrong endpoint URL, or a firewall blocking the
  egress. Provider-specific guidance lives in the provider's own
  runbook.
- `comparison` populated with regressions — investigate the slowest
  flagged stage first. `http_roundtrip` getting slower usually means
  the provider region is degraded; `pool_acquire` getting slower means
  pool exhaustion under load.

## Example: healthy report

```json
{
  "status": "healthy",
  "provider": {
    "name": "azure_foundry",
    "endpoint_hostname": "example-resource.openai.azure.com",
    "dimension": 1536
  },
  "timing": {
    "cold_ms": 412.0,
    "warm_p50_ms": 38.4,
    "warm_p95_ms": 71.9,
    "warm_p99_ms": 88.2
  },
  "transport": {
    "coalesce_ratio": 0.12,
    "cache_hit_rate": 0.41,
    "pool_acquire_p50_ms": 0.6
  },
  "stage_latency_ms": {
    "pool_acquire": 0.6,
    "coalesce_wait": 1.2,
    "cache_lookup": 0.3,
    "http_roundtrip": 34.1,
    "response_parse": 1.8
  },
  "tuning_recommendations": [],
  "exit_code": 0
}
```

## Example: degraded report

```json
{
  "status": "degraded",
  "provider": {
    "name": "openai",
    "endpoint_hostname": "api.openai.com",
    "dimension": 1536
  },
  "timing": {
    "cold_ms": 1820.0,
    "warm_p50_ms": 215.0,
    "warm_p95_ms": 2480.0,
    "warm_p99_ms": 3120.0
  },
  "transport": {
    "coalesce_ratio": 0.78,
    "cache_hit_rate": 0.02,
    "pool_acquire_p50_ms": 14.3
  },
  "stage_latency_ms": {
    "pool_acquire": 14.3,
    "coalesce_wait": 92.0,
    "cache_lookup": 0.4,
    "http_roundtrip": 1980.0,
    "response_parse": 2.1
  },
  "tuning_recommendations": [
    {
      "field": "pool_size",
      "current": 4,
      "suggested": 16,
      "rationale": "pool_acquire_p50_ms is 14.3 ms (target <2 ms); pool is the bottleneck under your concurrency"
    },
    {
      "field": "coalesce_window_ms",
      "current": 50,
      "suggested": 20,
      "rationale": "coalesce_ratio is 0.78 — most requests are waiting for batchmates that never arrive"
    },
    {
      "field": "cache_max_entries",
      "current": 1024,
      "suggested": 8192,
      "rationale": "cache_hit_rate is 0.02 under a repeated-query workload; the cache is undersized"
    }
  ],
  "exit_code": 1
}
```

## Field definitions

### `status` (string, required)

One of `"healthy"`, `"degraded"`, `"unreachable"`. The single
top-level verdict. See [Status thresholds](#status-thresholds) below
for how the probe decides which.

### `provider` (object, required)

| Field | Type | Notes |
|---|---|---|
| `name` | string | Registered provider name (`azure_foundry`, `openai`, etc.) — matches the `KAIRIX_PROVIDER` env var. |
| `endpoint_hostname` | string | **Hostname only**, never the full URL. `api.openai.com`, not `https://api.openai.com/v1/embeddings`. Privacy: report is intended to be sharable on a support issue, so paths/query/auth fragments are stripped. |
| `dimension` | integer | Embedding vector dimension. Constant per deployed model. Surfaces here so a support engineer can spot dimension mismatches without inspecting config. |

### `timing` (object, required)

End-to-end embed latencies, in milliseconds.

| Field | Type | Notes |
|---|---|---|
| `cold_ms` | number | First-request latency (cold connection pool, no cache). |
| `warm_p50_ms` | number | Median latency over the warm sample. |
| `warm_p95_ms` | number | 95th-percentile latency over the warm sample. |
| `warm_p99_ms` | number | 99th-percentile latency over the warm sample. |

### `transport` (object, required)

Universal endpoint-concern metrics; identical shape regardless of which
provider is configured.

| Field | Type | Notes |
|---|---|---|
| `coalesce_ratio` | number (0–1) | `batches / requests`. Near-0 means every request goes solo (window too short for the workload). Near-1 means many requests crowded into few batches (which is *good* under load, *wasteful* on solo workloads — the recommendation engine cares about workload shape, not the raw number). |
| `cache_hit_rate` | number (0–1) | Fraction of embed/query requests served from the in-process cache. |
| `pool_acquire_p50_ms` | number | Median time spent waiting for a pooled HTTP client. > a few ms means pool exhaustion. |

### `stage_latency_ms` (object, required)

Per-stage timing breakdown. **Keys are uniform across providers** —
adding a new provider does not change this section's schema. The probe
reads the `transport/telemetry/` hook described in the ADR; each layer
contributes its named stages.

| Stage key | Layer | Notes |
|---|---|---|
| `pool_acquire` | transport | Time waiting for an httpx client out of the pool. |
| `coalesce_wait` | transport | Time the request waited in the coalescer batch window. |
| `cache_lookup` | transport | Time to check the in-process embed/query cache. |
| `http_roundtrip` | provider | Wire time: serialize, send, receive, deserialize body bytes. |
| `response_parse` | provider | Time to parse the response body into the typed return shape. |

A provider that uses no coalescer (e.g. local Ollama with `pool_size=1`)
reports `coalesce_wait: 0.0` — the field is still present.

### `tuning_recommendations` (array, required)

Zero or more recommendation objects. Empty list when status is
`healthy`.

| Field | Type | Notes |
|---|---|---|
| `field` | string | Config field name to edit (e.g. `pool_size`, `coalesce_window_ms`, `cache_max_entries`). |
| `current` | number/string | Current configured value, as the operator would see it in their config. |
| `suggested` | number/string | Recommended new value. |
| `rationale` | string | One-sentence human-readable reason, citing the observed metric that triggered the suggestion. |

### `comparison` (object, optional)

Populated only when `--compare <baseline.json>` was passed.

```json
{
  "comparison": {
    "baseline_path": "/path/to/baseline.json",
    "baseline_collected_at": "2026-05-10T14:22:01Z",
    "regressions": [
      {
        "stage": "http_roundtrip",
        "baseline_ms": 34.1,
        "current_ms": 72.0,
        "percent_slower": 111.1
      }
    ]
  }
}
```

A stage appears in `regressions` only if it is **more than 20%** slower
than the baseline. Stages within 20% are within run-to-run noise.

### `error` (string, optional)

Populated only when `status` is `unreachable`. Human-readable description
of the failure. No stack traces, no secrets — this field is intended for
support-issue sharing.

### `exit_code` (integer, required)

| Value | Meaning |
|---|---|
| `0` | `status == "healthy"` |
| `1` | `status == "degraded"` |
| `2` | `status == "unreachable"` |

Mirrored at the process level so CI / shell scripts can branch on it
without parsing the JSON.

## Status thresholds

> **TODO (Wave 2 IM-9)** — the thresholds below are the SK-7 strawman;
> IM-9 will calibrate them against real probe runs and may move them.
> All thresholds live in one config table when IM-9 lands so a future
> change is a one-line edit, not a refactor.

| Verdict | Trigger (any one) |
|---|---|
| `unreachable` | every probe call errored, or the configured provider's `healthcheck()` returned a failure |
| `degraded` | `warm_p95_ms > 1000`, OR `pool_acquire_p50_ms > 5`, OR `coalesce_ratio > 0.7` under a workload the probe classifies as solo, OR `cache_hit_rate < 0.05` under the repeated-query phase |
| `healthy` | none of the above |

## Tuning-recommendation heuristics

> **TODO (Wave 2 IM-9)** — these heuristics are the SK-7 strawman.
> IM-9 will lock the trigger metrics and the `suggested` formulas into
> a single dispatch table (one row per (`field`, trigger) pair) so the
> ruleset is auditable and overridable.

| Recommendation `field` | Trigger | `suggested` formula |
|---|---|---|
| `pool_size` | `pool_acquire_p50_ms > 5` | `min(current * 4, 32)` |
| `coalesce_window_ms` | `coalesce_ratio > 0.7` under solo-workload phase | `max(current // 2, 5)` |
| `cache_max_entries` | `cache_hit_rate < 0.05` under repeated-query phase | `current * 8` |

Heuristics are deliberately coarse: the goal of probe-config is to
nudge the operator toward a sensible region, not to find the optimum.
For tuning at the optimum, the operator runs the PVT suite
(`kairix probe --suite reflib`) against a representative concurrency.

## Privacy and shareability

The report is intended to be shareable on a public GitHub issue or
support email. To keep that safe:

- Endpoints surface as **hostname only** (never path/query/auth).
- No credential fields, ever (no api keys, no bearer tokens, no
  managed-identity client IDs).
- No request/response bodies — only timings and counts.
- No file paths from the operator's machine in the `error` field;
  the baseline comparison includes the **path the user typed**, which
  is their choice to share.

## Related docs

- [Provider plugin architecture ADR](provider-plugin-architecture.md) —
  why the probe is singular and what the layering looks like.
- [Performance testing approach](performance-testing-approach.md) —
  PVT consumer side of the same probe.
- [Fitness functions](fitness-functions.md) — F29 (probe singularity).
