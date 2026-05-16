# SRE worker — design (closes #243 design phase)

> **Status**: design-only. No code in this design phase per #243 DoD ("No code yet — design first"). Implementation is split into three phases below; each gets its own implementation issue when this design is accepted.
>
> **Companion**: see [`operational-tests-design.md`](operational-tests-design.md) for the CLI primitives this worker invokes. The architectural invariant from that design — *the SRE worker is a scheduler over kairix CLI primitives, not a probe library* — shapes every decision below. Adding a new probe means adding a `kairix <subcommand>`, then wiring it into the worker's rotation; the worker itself doesn't grow.

## Why a separate worker at all

Three operational failure classes the current ingest worker either causes, hides, or can't address:

1. **Boot-time provisioning failures go silent.** The v2026.5.10.5 incident: `kairix-fetch-secrets.service` was disabled on the VM after a reboot. The ingest worker booted, found no credentials, looped failing embed attempts, and the only signal was a dogfood agent reporting "search broken" hours later. The failure was mechanically fixable from the host (`systemctl enable --now kairix-fetch-secrets`) — but nothing tried, because no part of the running system is responsible for "is the deployment provisioned correctly?"
2. **OTel has no home.** Wave-5 made the worker state observable via `WorkerState` counters and phase transitions. Exporting that as OTel meters/spans is high-leverage, but bolting metrics-exporter onto the ingest worker breaks the worker's role (ingest is product-logic; metrics-export is operational plumbing).
3. **No healthcheck endpoint.** Docker-compose healthchecks, k8s probes, and external monitors have no surface to poll. `kairix worker status` reads a state file; each downstream parser independently — wrong.

These are SRE concerns. They share a lifecycle (continuous, restart-on-crash, no user-facing surface) and a data shape (observe state → optionally remediate → emit signal). They do **not** share a lifecycle with embed / wikilinks / entity-seed / canary work.

## In-scope / out-of-scope

In-scope (this worker, eventually):
- Pre-flight provisioning verification (systemd units, docker services, secrets present-and-active)
- Continuous secret freshness detection
- OTel emission (meters from `WorkerState`, spans on phase transitions)
- Healthcheck surface (HTTP `/healthz` and `/healthz/ready`)
- Onboard-check parity (run the same probes continuously the CLI runs once)
- Bounded autonomous remediation (whitelist of safe `systemctl enable` actions; everything else → alert-only)

Out-of-scope (stays in the ingest worker or stays a CLI):
- Embedding, wikilinks, entity-seed, recall canary work — that's the ingest worker
- Bulk document operations
- One-shot operator commands (`kairix benchmark`, `kairix embed --limit N`)
- Replacing the ingest worker (these are complementary, not substitutional)

## Architecture decisions

Each decision below has the question, the options considered, the chosen path, and the rationale. Where a decision is conditional on a phase, it's marked.

### 1. Process model

**Options**:
- **Separate process** (systemd unit, docker container): clean isolation; crash of one doesn't take the other; ops surface is two units, not one.
- **Thread inside the ingest worker**: lower ops surface; shares memory cheaply; but a crash in either side kills both, and pre-flight self-heal needs to run *before* the ingest worker is healthy enough to host a thread.
- **Separate Python module, same process, lifecycle-managed**: marginal gain over thread model; same coupling problem.

**Chosen**: separate process. The boot-failure incident proves the point: pre-flight needs to run before the ingest worker can be trusted to host anything, so the SRE component cannot live inside the worker it diagnoses. On docker-compose this is `kairix-sre` as a sibling service; on systemd it is `kairix-sre.service` as a sibling unit.

**Rationale**: the alpha-deploy webhook (`services/alpha-deploy-webhook/`) already established the "separate small process" pattern for SRE-adjacent work, and our Go-integration plan ([go-integration-plan.md](go-integration-plan.md)) reserves Go for exactly this slot. Process isolation is cheap; coupling is the expensive choice.

**Language**: Python for phase 1 (sharing the existing state-file readers and onboard-check probes is the leverage). Optionally migrate the healthcheck-and-OTel surface to Go in phase 4 if Python's startup cost on a docker-compose healthcheck poll proves measurable. Phase 1 stays in Python.

### 2. State coordination

**Options**:
- **Read-only**: SRE worker only consumes `WorkerState`; never mutates it. Ingest worker is the sole writer.
- **Read + write own counter**: SRE worker adds a separate state file (`/var/lib/kairix/sre-state.json`) for its own counters; never touches `worker-state.json`.
- **Read + write shared file**: SRE worker writes a `sre_phase` field into `worker-state.json`.

**Chosen**: read + write own counter file. Two state files, one writer each, no locking concerns.

**Rationale**: shared-write is the failure mode (whose schema owns the file? what happens during phased rollouts where the readers don't know about the new field?). Two files keeps single-writer invariant intact and makes the OTel exporter trivially "open both, emit both" — no locking, no schema-migration ladder.

### 3. OTel target

**Options**:
- **No default; operator must configure**: minimum-surprise; but no metrics ship in stock deployments and that's the point of OTel.
- **Embed an otel-collector container in docker-compose**: opinionated; complete out-of-the-box; doubles the docker-compose footprint.
- **stdout/JSON exporter by default + configurable OTLP endpoint**: ship signal even with no collector; operators who add `OTEL_EXPORTER_OTLP_ENDPOINT=...` get push-to-collector for free.

**Chosen**: stdout/JSON exporter by default + OTLP override. The stdout exporter writes to the worker's journald/docker-logs stream, which every operational stack already collects.

**Rationale**: signal-by-default beats opinionation. The OTLP override path is one env var. The docker-compose footprint of an embedded otel-collector is not justified for a deployment that may not run a metrics backend.

### 4. Healthcheck protocol

**Options**:
- **HTTP `/healthz`**: docker-compose `HEALTHCHECK CMD curl -f http://localhost:9090/healthz`; k8s liveness probe HTTP; external monitor poll.
- **Unix socket**: lower attack surface; awkward from docker-compose healthcheck.
- **Sentinel file** (touch on success): cheapest; no liveness signal — a hung process keeps touching with a stuck watchdog.
- **CLI subcommand** (`kairix sre status`): same as `kairix worker status` today — every reader reparses.

**Chosen**: HTTP `/healthz` (liveness; cheap) + `/healthz/ready` (readiness; runs the onboard-check probe subset). Bound to `127.0.0.1` only by default — fronted by cloudflared/nginx on VMs where external exposure is desired. Reuses the binding shape from the alpha-deploy webhook.

**Rationale**: this is the docker-compose / k8s / Prometheus blackbox-exporter universal lingua. The Unix socket is theoretically nicer but practically every consumer wants HTTP. Bind to loopback to keep "no public exposure" the default.

### 5. Remediation policy

**Options**:
- **Alert-only**: never act autonomously; surface all problems to the operator.
- **Whitelist of safe actions**: hardcoded list of remediations the worker is allowed to attempt (e.g., `systemctl enable --now kairix-fetch-secrets`). Everything else → alert-only.
- **Policy file**: operator-configurable list.

**Chosen**: whitelist of safe actions, phased rollout. Phase 1: alert-only (no remediation at all). Phase 3 (after one quarter of phase-1 telemetry confirms the alert classes): introduce a tightly-bounded whitelist where every entry has a Why-this-is-safe rationale, a Why-rollback-is-trivial rationale, and a unit test that sabotage-proves the remediation actually fixes the failure mode it claims to fix.

**Forbidden actions, ever**:
- `systemctl disable` (only enable; never disable autonomously)
- Anything that touches `/run/secrets/*` (rotation is `kairix-fetch-secrets`'s job)
- `docker compose down` / `up` (operator territory; the SRE worker can't bring services back if it brings them down)
- `git pull` / `pip install` / package upgrades (release-flow territory)
- Filesystem writes outside `/var/lib/kairix/sre-state.json` and its log directory

**Rationale**: mis-remediation hides real problems. The alpha-deploy incident is recoverable in seconds when the operator sees the alert; un-recoverable when the SRE worker did the wrong thing autonomously and overwrote the symptom. Start alert-only, earn the right to act.

### 6. Privilege model

**Options**:
- **Run as root**: simplest; broadest blast radius.
- **Dedicated user with sudo helper** (`kairix-sre` user, narrow sudoers entry for the whitelisted `systemctl enable` commands): blast radius bounded by sudoers spec.
- **Privileged docker container**: equivalent of root inside a sandbox; still root.
- **Capability-based**: granular Linux capabilities (e.g., `CAP_SYS_ADMIN` for systemd D-Bus); supported but operationally novel.

**Chosen**: dedicated `kairix-sre` system user with a narrow sudoers fragment that allows only the whitelisted `systemctl enable --now <unit>` commands. Mirrors the alpha-deploy-webhook user pattern.

**Rationale**: phase-1 alert-only doesn't need any of this — the worker runs as `kairix-sre` and reads state. The sudoers entry is added only when phase-3 ships, and only for the specific units in the remediation whitelist. Bandit and operator-review can audit the sudoers fragment statically — that's the win over a privileged container.

### 7. Failure semantics — who watches the watchman

**Options**:
- **systemd `Restart=on-failure`** + alert on systemd failure-state via existing infra
- **A separate watchdog process** (recursive — the previous answer doesn't scale)
- **External liveness probe** (cron, blackbox-exporter, GitHub Actions ping)

**Chosen**: systemd `Restart=on-failure` with `RestartSec=30s` and `StartLimitBurst=5` / `StartLimitIntervalSec=600` to break crash loops. External liveness monitor (the existing kairix dogfood alert flow) catches "SRE worker hard-down for > 10 min". Combined with phase-1 being alert-only, the worst case is "alerts stop arriving" — the operator notices via the dogfood loop.

**Rationale**: standard systemd restart semantics are battle-tested. The recursive watchdog problem is real; pushing the outermost watcher off the host (to the existing dogfood-agent flow) is the cheap exit.

### 8. Observability when the observer is down

**Options**:
- **Centralise OTel in the SRE worker** — ingest worker's state-file is the source, SRE worker reads + emits. When SRE is down, no metrics.
- **Emit from both processes** — ingest worker emits its own counters, SRE worker emits its own + does aggregation.
- **stdout-emit from both, central aggregation external** — neither process exports OTLP; both log structured JSON; aggregator is an OTel collector reading both log streams.

**Chosen**: emit-from-both, both via the stdout/OTLP path established in decision 3. The ingest worker exports counters that describe ingest-work (chunks embedded, errors); the SRE worker exports counters that describe SRE-state (probes-passed, probes-failed, remediations-attempted, alerts-emitted). They never overwrite each other because the metric namespaces are disjoint (`kairix.ingest.*` vs `kairix.sre.*`).

**Rationale**: centralising metrics in a process whose job is to be the bellwether for system health creates a single-point-of-failure where the bellwether's silence and the silence of every other metric are indistinguishable. Disjoint namespaces and separate emitters keep ingest-worker observable even when SRE is down — and vice versa.

## Migration story for `kairix onboard check`

The CLI stays exactly as it is, forever. The SRE worker imports the same probe functions from `kairix.core.health`, runs them on a schedule, and exposes the results via `/healthz/ready`. There is no deprecation, no flag day, no "use the worker instead of the CLI" guidance. They coexist by design:

- Operators run `kairix onboard check --json` for ad-hoc validation (post-deploy, post-rotation, post-incident).
- The SRE worker runs the same probes every 60s for continuous monitoring.
- Both are sources of truth for the same underlying questions; both will agree because they share the probe implementations.

The runbook surfaces ([kairix-retrieval-health.md](../runbooks/kairix-retrieval-health.md) and the upcoming SRE runbook) will lead with `kairix onboard check --json` as the human-facing entry point and document `curl http://127.0.0.1:9090/healthz/ready` as the machine-facing equivalent.

## Phased rollout

### Phase 1 — Healthcheck only (alert-only; no remediation)

**Ship**:
- `kairix-sre` Python module under `kairix/sre/`
- Process model: separate systemd unit + docker-compose service
- HTTP `/healthz` (liveness; returns 200 if the process is up) and `/healthz/ready` (runs `kairix.core.health.run_all_checks()` and returns 200 only when all green; 503 otherwise with the failed-check envelope in body)
- Structured-log alerts (JSON to stdout) when `/healthz/ready` flips green→red, with rate limiting (no more than 1 alert per check transition; debounce 60s)
- New `sre-state.json` writer with counters: `probes_passed`, `probes_failed`, `last_probe_ts`, `alert_count`

**Acceptance criteria**:
- A deployment that intentionally disables `kairix-fetch-secrets.service` and reboots emits a structured-log alert within 90s.
- A deployment with all probes passing keeps `/healthz/ready` at 200 indefinitely under a sustained 1-poll-per-30s load.
- Crash-loop the SRE worker → systemd restarts it 5 times, then enters `failed` state. Recovery: `systemctl reset-failed kairix-sre && systemctl start kairix-sre`.
- New runbook lives at `docs/runbooks/kairix-sre-worker.md` and is linked from the runbooks README.

**Tests**:
- Unit: probe-runner sabotage-proofs (mock each probe to fail, assert alert envelope shape).
- Integration: HTTP handler tests with a real probe runner against a FakePaths fixture.
- BDD: `Feature: SRE worker emits alerts on probe failure transitions`.

### Phase 2 — OTel emission

**Ship**:
- OpenTelemetry metrics emitter integrated into the probe runner.
- Default exporter: stdout/JSON.
- Configurable OTLP exporter via `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS` env vars.
- Metric namespace: `kairix.sre.*`.

**Acceptance criteria**:
- A 5-minute run with the stdout exporter produces one structured record per metric per probe-cycle.
- Pointing at a local `otelcol --config=basic.yaml` (Prometheus exporter at `:8889`) shows the metrics on `:8889/metrics`.

**Tests**:
- Unit: metric-shape assertions (each metric has a defined unit, description, and attribute set).
- Integration: emit-to-stdout test that captures the JSON-records and asserts the schema.

### Phase 3 — Bounded autonomous remediation (gated on phase-1 telemetry review)

**Ship**:
- Per-failure-class remediation whitelist in `kairix/sre/remediations.py`. Phase 3 launches with exactly **one** remediation entry: `systemctl enable --now kairix-fetch-secrets.service` for the boot-failure incident class.
- Sudoers fragment installed at `/etc/sudoers.d/kairix-sre`.
- Rate limit: at most one remediation per failure-class per 1h window. Repeated failure of the same class after the window → escalate to alert-only.
- All remediation attempts logged with before/after probe envelopes.

**Acceptance criteria**:
- The v2026.5.10.5 incident scenario (reboot with `kairix-fetch-secrets` disabled) auto-heals within 90s.
- Manual sabotage of the sudoers fragment (remove the entry) → remediation fails-closed with a structured error, alert still fires.

**Gating**: phase 3 cannot start until phase 1 has produced ≥30 days of telemetry showing alert classes are accurately identified. If phase-1 alerts include false positives we cannot remediate them automatically.

**Tests**:
- Unit: remediation-whitelist boundary tests (every non-whitelisted action rejected).
- Integration: end-to-end remediate-and-verify with a fake systemd D-Bus shim.
- BDD: `Feature: SRE worker auto-heals fetch-secrets disabled-on-boot`.

## Open questions deferred to implementation

These are intentionally not decided here because they are pure implementation choices that don't change the architecture:

- Exact OTel SDK choice (opentelemetry-sdk + opentelemetry-exporter-otlp-proto-grpc is the obvious default).
- Probe cadence (60s seems right; needs to be confirmed against actual probe runtime — `kairix onboard check --json` budget is ~3s today so 60s gives headroom).
- Alert delivery beyond stdout-JSON (PagerDuty, Slack, email) — Phase 1 ships stdout only; downstream is operator-configurable via log shipper.

## Risks and how this design addresses each

| Risk | Mitigation in this design |
|---|---|
| SRE worker masks real problems via mis-remediation | Phase 1 is alert-only; Phase 3 whitelist starts at 1 entry; every entry has sabotage-proof tests; rate limit on repeated attempts |
| Recursive "who watches the watchman" failure mode | systemd Restart=on-failure with crash-loop break; external (dogfood-agent) liveness check is the outermost layer |
| Centralised observability collapses when observer crashes | Disjoint metric namespaces; ingest worker emits its own counters independently |
| Privilege escalation from a compromised SRE worker | Narrow sudoers fragment; phase-1 has no sudoers entry at all; allowlist of `systemctl enable --now <whitelisted-unit>` only |
| OTel-exporter dependency drift breaks the worker | Default stdout exporter has zero exporter dependencies; OTLP exporter is optional and feature-flagged |

## What this design does not commit to

- A specific OTel collector deployment shape (push-to-SaaS vs. local collector vs. nothing) — Phase 1 emits stdout, the rest is operator config.
- Cross-host SRE (one worker watching multiple kairix deployments) — each deployment runs its own SRE worker; cross-host aggregation is the operator's collector's job, not ours.
- Replacing `kairix onboard check` — it stays.
- Replacing the ingest worker — it stays; these are siblings.

## Related

- [#243](https://github.com/three-cubes/kairix/issues/243) — this issue
- [go-integration-plan.md](go-integration-plan.md) — when Go is appropriate (relevant if phase-4 migrates the healthcheck-and-OTel surface to Go later)
- [kairix-retrieval-health.md](../runbooks/kairix-retrieval-health.md) — the operator-facing runbook this worker complements
- [alpha-deploy-webhook](https://github.com/three-cubes/kairix/tree/main/services/alpha-deploy-webhook) — the existing pattern for a small separate-process operational binary
