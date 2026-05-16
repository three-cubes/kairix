# Operational tests as CLI primitives — design (closes #276 design phase)

> **Status**: design-only. No code in this phase. Implementation issues file separately per Tier once accepted.
>
> **Companion**: this design is the test-tooling complement to [`sre-worker-design.md`](sre-worker-design.md). The SRE worker doesn't implement health checks itself — it invokes the primitives defined here.

## Why the separation

Today's alpha-validation work (#272 Phase 4) and the regression it caught (#275) made the rule explicit: **a test that exists only to manage versioned change belongs in CI; a test that has any operational use — periodic health, on-demand diagnostic, post-incident probe — must be a kairix CLI primitive shipped in the binary.** The principle:

> If the only valid use case for a test is to manage versioned change as part of the SDLC, it stays in `tests/` and runs only in CI. If it has any operational use, it ships as a `kairix <subcommand>` and is invokable identically by the SRE worker, CI workflows, and ad-hoc operator commands.

This is the natural extension of the existing pattern (`kairix benchmark run`, `kairix onboard check`, `kairix store crawl`) which are already operational primitives reused across surfaces — they didn't grow into tests *after* they were CLI commands; they were CLI commands *because* they're operational.

The previous draft of #276 conflated these. This design separates them.

## What goes where

### SDLC-only (stays in `tests/`, CI-only)

- `tests/unit/`, `tests/integration/`, `tests/contracts/`, `tests/bdd/` — correctness
- `mypy --strict` — type correctness
- Architecture fitness functions (F1-F24, G1-G10) — design correctness
- `ruff check` / `ruff format` — style correctness
- `detect-secrets`, confidential-pattern check — supply-chain correctness

These never run in production, never need to be invoked by the SRE worker, never give meaningful signal at runtime. They gate code into develop. That's their entire job.

### Operationally-relevant (CLI primitive in the kairix binary)

Existing examples:
- `kairix onboard check` — health probes; SRE worker calls this on a cadence
- `kairix benchmark run --suite reflib` — retrieval quality; alpha gate calls this
- `kairix store crawl` — graph rebuild; operator calls this after corpus changes
- `kairix worker status` — runtime state inspection
- `kairix embed --limit N` — incremental embed; operator calls this for triage
- `kairix embed rebuild-fts` — FTS recovery; runbook routes operators to this

New commands proposed below — each one earns its CLI slot by being needed in *more than one place*. If a test is only valuable in CI, it stays in `tests/`.

## The new operational test commands

Each command:
1. Is a `kairix <subcommand>` (dispatch entry in `kairix/cli.py`).
2. Accepts `--json` for machine output; defaults to human-readable.
3. Exits `0` on pass, `1` on fail, `2` on indeterminate (e.g., couldn't read baseline).
4. Respects `--timeout` (operator can cap long-running probes).
5. Reads config from `KAIRIX_CONFIG_PATH` like every other command (no hidden state).
6. Has a dedicated runbook section so a failure has a triage path.

### `kairix soak run`

Repeats a workload and asserts the system holds together across iterations. Catches the #275 class of bug — work that's individually fine but degrades when repeated.

```
kairix soak run --suite reflib --repeat 3 [--max-memory-growth-mb 50] [--max-log-volume-mb 5] [--json]
```

Assertions:
- Memory delta per iteration < `--max-memory-growth-mb` (default 50 MB)
- Per-iteration wall time within 20% of first iteration (no degradation)
- Total stderr bytes < `--max-log-volume-mb` × repeat (catches log-spam regressions)
- No new file descriptors held at exit
- Per-iteration `BenchmarkResult` is byte-identical (deterministic — proves no cross-iteration state leakage)

JSON envelope:
```json
{
  "suite": "reflib",
  "repeat": 3,
  "iterations": [
    {"weighted_total": 0.907, "duration_s": 142, "memory_mb": 312, "stderr_bytes": 4096, "fds": 23},
    ...
  ],
  "passed": true,
  "failures": []
}
```

Invokers:
- **SRE worker**: nightly soak run on the reflib suite; alerts if any iteration fails the assertions even when individual benchmark runs would pass.
- **alpha-deploy webhook**: optional second-line check after `benchmark run`. If `benchmark run` passes but `soak run --repeat 2` fails, the alpha has a degradation-under-load bug — exactly the symptom that broke `v2026.5.16a1`.
- **CI**: a "Tier 1 soak" job that runs against a representative subset (`--suite reflib --repeat 2`) for every release-alpha dispatch.
- **Operator**: post-incident "is this actually fixed under repeat?" check.

### `kairix probe search`

Concurrent-load + latency probe. The kairix-side analogue of HTTP load testing.

```
kairix probe search --concurrency 10 --queries 100 [--query-mix builtin|file:path] [--json]
```

Assertions:
- p99 latency < threshold (default 5s; configurable in `kairix.config.yaml`)
- Zero exceptions (any unhandled `Exception` fails the probe)
- No deadlocks (5min hard timeout per invocation)

Invokers:
- **SRE worker**: every 6 hours, smoke-test that retrieval is responsive under modest concurrency.
- **CI integration suite**: at PR time, against the fakes — proves the search code path doesn't have a regression that surfaces only under concurrency.
- **Operator**: when a dogfood agent reports "kairix is slow", run this and see if latency degraded or if the agent's client is the bottleneck.

### `kairix probe log-volume`

Asserts that running a workload doesn't produce more than X bytes of stderr per N cases. The kairix-side encoding of the #275 lesson.

```
kairix probe log-volume --suite reflib [--max-bytes-per-case 100] [--json]
```

Invokers:
- **CI** (Tier-1 soak job) — assertion fails when a future warn-per-case sneaks in.
- **alpha-deploy webhook** — optionally chained after `benchmark run` so the gate fails on log-spam regressions BEFORE the operator gets paged.

### `kairix probe factory-calls` (lower priority, possibly future)

Counts how many times `build_search_pipeline` (or other expensive factory) is called during a workload. Catches O(N) hidden costs.

```
kairix probe factory-calls --suite reflib --max-calls 1
```

Lower priority because the underlying #275 deeper fix (cache the pipeline across cases) is what this tests for — pin it once the fix lands, but not before.

## How the SRE worker (#243) uses these

The SRE worker design specifies probe runs every 60 seconds and remediation actions on a whitelist. The probes ARE these CLI commands:

```python
# Inside SRE worker (proposed phase 1)
class ProbeRunner:
    def liveness(self) -> ProbeResult:
        # kairix --version + 200 on /healthz
        ...

    def readiness(self) -> ProbeResult:
        # kairix onboard check --json
        return self._run_cli(["kairix", "onboard", "check", "--json"], expect_json=True)

    def soak(self) -> ProbeResult:  # Phase 2 — added when the soak primitive ships
        return self._run_cli(["kairix", "soak", "run", "--suite", "reflib", "--repeat", "2", "--json"])

    def latency(self) -> ProbeResult:  # Phase 2
        return self._run_cli(["kairix", "probe", "search", "--concurrency", "5", "--queries", "50", "--json"])
```

The SRE worker doesn't ship its own probe library. It composes the CLI primitives via subprocess (matching the existing pattern in the alpha-deploy webhook). When a new probe is needed:

1. Build it as a kairix subcommand first (it lands in the binary, becomes operator-invokable, gets a runbook section).
2. Wire it into the SRE worker's rotation second.

This is the architectural invariant: **the SRE worker is a scheduler over CLI primitives, not a library**. Adding a probe doesn't require redeploying the SRE worker — it ships with the next kairix image release.

## How CI uses these

The alpha-validation chain is the proving ground. After this design lands:

```yaml
# .github/workflows/release-vm-deploy.yml — extended (Phase 2)
jobs:
  deploy-vm: { ... }
  poll-alpha-gate:        # existing — checks vm-reflib-regression status
    needs: deploy-vm

  poll-alpha-soak:        # new — adds Tier-1 soak as a separate gate
    needs: deploy-vm
    steps:
      - run: |
          # Webhook posts a second commit-status: vm-reflib-soak
          # Poll for it the same way poll-alpha-gate does, with
          # a separate context name so the two failure modes are
          # diagnosable independently.
```

The webhook's `deploy.Service` orchestrates:
```go
benchmark()   // existing, posts vm-reflib-regression
soak()        // new, posts vm-reflib-soak
```

Both must be green for the alpha to clear. `check-alpha-gate` in `release.yml` becomes a two-context check.

## How ad-hoc operator invocation works

Operator runs `kairix soak run --suite reflib --repeat 3` on the VM (or in any kairix container) to:
- Confirm a fix actually fixed the load-fragility, not just the unit case
- Reproduce a dogfood-reported regression locally
- Validate a config change (e.g., new fusion strategy) doesn't degrade under repeat

Output is human-readable by default with a structured `--json` mode for piping into a triage tool. Every failure includes a `next:` line per the F21 affordance rule (already enforced for `scripts/checks/check_*`).

## Architectural invariants

These keep the CLI surface from rotting into a kitchen sink:

1. **Operationally-relevant test → CLI primitive**. No exceptions. If the SRE worker would want to invoke it, it's a `kairix <subcommand>`. If only CI wants it, it stays in `tests/`.
2. **Stateless invocations**. Each command runs in a fresh process; no shared state across calls. The SRE worker can call them concurrently without coordination.
3. **Single config source**. All commands read `KAIRIX_CONFIG_PATH` (or its defaults). No hidden env-var configs.
4. **JSON-or-text duality**. `--json` for SRE worker / CI / scripting; default text for humans.
5. **Exit-code semantics**. `0` pass, `1` fail, `2` indeterminate. Matches existing kairix conventions.
6. **Timeouts mandatory on long-running probes**. The SRE worker enforces its own, but every command must respect `--timeout` so a hung probe doesn't poison its scheduler.
7. **Runbook section per command**. Every new CLI primitive lands with a triage section in `docs/runbooks/`. Failure-modes-without-triage are the bug class this whole design exists to prevent.

## Phased rollout

### Phase 1 — soak primitive (closes the immediate #275 gap)

Ships:
- `kairix soak run --suite <name> --repeat N` CLI command in `kairix/quality/soak/cli.py`.
- Wired into the CI Tier-1 job — runs against a 20-case reflib subset for every alpha cut.
- Runbook section in `docs/runbooks/kairix-retrieval-health.md`.
- Integration with the alpha-deploy webhook as a separate `vm-reflib-soak` commit-status.

Acceptance:
- Reproducing #275 (pre-dedup state): `kairix soak run --suite reflib --repeat 2` fails with `log_volume_exceeded`, exit 1.
- Post-dedup state: same command passes.
- Sabotage-proof test in `tests/quality/soak/test_cli_unit.py` mutates the dedup back to per-call and confirms the soak assertion fails.

### Phase 2 — probe primitives (latency + log-volume)

Ships:
- `kairix probe search` (concurrent load)
- `kairix probe log-volume` (stderr-per-case assertion)
- Wired into the SRE worker's 6-hourly probe rotation (when SRE worker Phase 1 ships).
- Wired into CI integration suite.

Acceptance:
- `kairix probe search --concurrency 10 --queries 100` produces a JSON report with p50/p95/p99 latencies, exit 0 when under threshold.
- Loading a synthetic 50× slower fake fails the threshold assertion.

### Phase 3 — burst + failure-injection (defensive)

Ships:
- `kairix probe burst --qps 100 --duration 30s` (sustained-burst behaviour)
- `kairix probe stability --duration 1h` (long soak)
- Failure-injection harness in `tests/soak/` for Azure-embed-503, neo4j-restart, disk-pressure scenarios.

These ship after Phase 1 and 2 have a quarter of telemetry. Mirrors the SRE worker's "earn the right to act" principle: prove the framework works at smaller scope before adding load that costs real money to run.

## Out of scope (deliberately)

- **Pure micro-benchmarks** (pytest-benchmark style). Useful but solve a different problem. Maybe later.
- **Replacement for the existing `kairix benchmark run`**. That measures retrieval *quality*; soak/probe measure system *health*. Both stay; they're complements, not substitutes.
- **Distributed load**. Single-host first; cross-host is a separate concern that depends on the SRE worker design's "remote-mode" extension.
- **A separate test framework**. The CLI commands ARE the test framework. They reuse the existing test fixtures from `tests/fakes.py` where helpful, but the production code path runs through the CLI just like any other operator-invoked command.

## Risks and how this design addresses each

| Risk | Mitigation |
|---|---|
| CLI surface bloats with operational-test commands | Invariant 1 (must be reused by SRE worker AND CI AND operator); single-use CI-only tests stay in `tests/` |
| Soak/probe commands accidentally hammer production | Default to safe parameters (low concurrency, small repeat); operator must opt into heavier load via flags |
| SRE worker depends on a specific kairix version's CLI surface | Each probe command versions its JSON output schema (`schema_version: 1`); SRE worker pins the version it expects |
| New probe primitive ships before its runbook section | F23-style gate in CI: every new `kairix <subcommand>` requires a `docs/runbooks/kairix-<subcommand>.md` section |
| Probe assertion thresholds drift without anyone noticing | Phase 2 ships a baseline file (`benchmark-results/probe-baselines.json`) like the existing benchmark baselines, with the same per-release versioning (#271) |

## Related

- [#276](https://github.com/three-cubes/kairix/issues/276) — this issue
- [#275](https://github.com/three-cubes/kairix/issues/275) — the regression that motivated this design
- [#243](https://github.com/three-cubes/kairix/issues/243) — SRE worker (the primary consumer of these primitives)
- [`sre-worker-design.md`](sre-worker-design.md) — companion design; specifies the SRE worker as a scheduler over the CLI primitives defined here
- [#272](https://github.com/three-cubes/kairix/issues/272) — alpha-validation chain (the proving ground for Phase 1)
