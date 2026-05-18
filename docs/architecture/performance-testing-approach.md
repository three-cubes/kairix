# Performance testing approach — four layers, PVT explicitly opt-in

> **Status**: accepted. This document names what each kairix test layer
> actually measures, where in the lifecycle each runs, and why the in-process
> probes that ship today are NOT the production-latency gate. The decision
> is to ship PVT (Production Verification Testing) as an opt-in layer driven
> against the live deployed system, *not* as an upfront blocker on every PR.

## Context

The Phase 2 + Phase 3 work landed `kairix probe search` and `kairix probe
burst` (see [`teaming-concurrency-strategy.md`](teaming-concurrency-strategy.md)
and [`operational-tests-design.md`](operational-tests-design.md)). The live
VM verification on `v2026.5.16a4` and `v2026.5.16a5` surfaced a methodology
gap: the probe CLI spawns a fresh subprocess that pays a cold factory-build
tax (~4-5 s) before any query completes. Real teaming traffic doesn't see
that — agents call a long-running MCP server whose factory cache is already
warm.

That made the conclusion explicit: **the in-process probe measures the
Python-pipeline regression surface, NOT what an agent over MCP experiences**.
Both numbers are useful — they answer different questions — but conflating
them in one tool produced misleading headline results (a 58.7 % qps drop
that was mostly cold-start dilution).

This document names the four layers and what each is for. Future tests
(and existing ones being refactored) target the right layer explicitly.

## Decision

**Four test layers, three CI-gating, one opt-in for production verification.**

| layer | transport | data | runs in CI | catches |
|---|---|---|---|---|
| **unit** | none (in-process function call) | fake | ✅ every push | Python regression — composition, fusion, percentile calc, intent classification |
| **bdd** | none (in-process function call) | fake | ✅ every push | agent-shaped semantics — does the envelope look right; does the tool refuse bad input gracefully |
| **integration** | in-process FastMCP routing | fake | ✅ every push | wrapper-closure dispatch + JSON envelope shape; cross-component wiring |
| **PVT** | **real HTTP MCP JSON-RPC** | **live deployed server** | **❌ opt-in, operator-invoked** | **agent-experienced latency + transport behaviour + warm-cache reality** |

The first three are bounded by the CI minute budget — fast (<10 min for the
full battery), deterministic, fake-backed. The PVT layer is where production
truth lives; it doesn't gate PRs and runs against a live VM via an opt-in
operator command (or a scheduled SRE run, when that lands).

## Why PVT is not in CI

Three reasons, in priority order:

1. **PVT measures the real system, not a synthetic one.** The whole point is
   to capture what an agent experiences against the deployed MCP server with
   real Azure embed calls, real vault size, real network roundtrip. None of
   that can be reproduced inside a CI runner without recreating the entire
   deployment — at which point you're not measuring production any more.
2. **PVT is slow and resource-bound.** A meaningful run is a multi-minute
   real-load test against the deployed VM. CI runs should stay under 10 min
   for the whole battery; PVT belongs on a separate cadence (post-deploy
   smoke, weekly cron, on-demand triage).
3. **PVT failures are operational, not code regressions.** A PVT fail says
   "production is slow today" — possibly Azure regional latency, possibly
   vault size, possibly a Tier 1 lever needing tuning. None of those should
   block a PR merge. They should produce an actionable runbook hit, not
   bounce a code review.

The existing `kairix probe search` / `kairix probe burst` CLIs are the PVT
toolkit. They were initially framed as "performance gates" but live
verification showed they measure the Python-pipeline regression surface
(useful as a regression gate, but not as a production-latency gate). Both
roles get clean homes here.

## Layer details

### Layer 1 — unit

Already covered by the existing `@pytest.mark.unit` set. Per-module tests
that exercise one function or class with fakes at the boundary. The kairix
seam pattern — Protocol-shaped dependencies, FakeNeo4j, FakeRetriever — is
the unit-layer contract. F7 enforces ≥90 % per-file coverage.

The recent `kairix.quality.probe.clients` refactor (commit `54ac358c`)
moved the probe's `searcher` seam onto a named `SearchClient` Protocol.
Unit tests use `FakeFastSearchClient` / `FakeSlowSearchClient` classes
implementing that Protocol; the same Protocol is what PVT's future
`MCPHttpSearchClient` will satisfy.

### Layer 2 — BDD

`tests/bdd/features/*.feature` with `pytest-bdd`. Gherkin scenarios that
describe agent-shaped behaviour at the public surface of each capability.
The step definitions use fakes at the search-backend / store / graph
boundary so scenarios run fast and deterministically. F12 / F13 enforce
that every feature has a happy-path scenario and that scenarios don't
leak implementation symbols.

BDD is the **semantic-correctness gate** — does the envelope an agent
receives match what the contract promises? Does an unknown topic produce
a structured error, not an exception? Does the tool refuse bad input
gracefully?

BDD is NOT a performance gate. The in-process call path bypasses the MCP
transport that adds milliseconds in production. Don't expect BDD numbers
to predict production latency.

### Layer 3 — integration

`tests/integration/test_*.py`. Cross-component tests that wire two or more
real kairix components together with fakes only at the system boundary
(Azure, Neo4j, vault filesystem). The existing
`tests/integration/test_mcp_build_server.py` is the reference shape — it
constructs the real `build_server()` and dispatches through FastMCP's
in-process routing.

Integration tests catch the wrapper-closure / envelope-shape regressions
that unit tests miss (because unit tests don't exercise the build_server
closure path). They run in CI under the `integration` flag.

Integration is NOT a transport test. FastMCP's in-process routing is
function-call dispatch, not HTTP framing.

### Layer 4 — PVT (opt-in, operator-invoked)

`tests/pvt/features/*.feature` with the `@pytest.mark.pvt` marker. Scenarios
that describe **agent-experienced behaviour** against a real running MCP
server. Steps drive `MCPHttpSearchClient` (or equivalent transport-specific
clients) and assert on what the client sees over the wire.

`pytest.mark.pvt` is autoskipped unless `KAIRIX_PVT=1` is set in the
environment. The marker exists to keep the scenarios in the repository as
authoritative spec without forcing them to run on every CI invocation.

The PVT layer is invoked by:

- **Operators**, ad-hoc, to triage "is production slow today" questions.
  Run with `KAIRIX_PVT=1 pytest tests/pvt/ -m pvt --pvt-target=https://your-kairix-host.example.com/mcp`.
- **The release pipeline**, post-deploy, to verify alpha tags against the
  live deployment before promoting. Wired into the alpha-deploy webhook
  as a follow-on step (future).
- **The SRE worker**, on a cadence, when the SRE worker design (#243)
  lands. Same scenarios, scheduled cadence, structured fault reports.

The 5 starter scenarios shipped at the time of writing:

1. `agent_cold_start_experience` — cold container → agent receives `ColdStart`
   envelope within 100 ms, not a hung connection.
2. `agent_warm_baseline` — warm MCP server → single agent `tool_search`
   call p95 ≤ 500 ms over 50 sequential calls.
3. `teaming_load_experience` — 10 simulated agents (separate MCP clients)
   each issue 5 queries over 60 s → p95 across-all-clients ≤ 500 ms, no errors.
4. `session_start_storm` — 20 agents connect simultaneously, each issues
   2 queries → no failed connections, p99 ≤ 2 s.
5. `in_session_stability` — one agent, 30 queries spaced 1 s apart over
   30 s → latency at query 30 within 20 % of latency at query 1.

The harness behind these scenarios (server fixture + MCP HTTP client) is
tracked under **#284**. Until #284 ships, the step definitions raise
`pytest.skip("PVT harness not yet built — see #284")` so the scenarios are
visible spec but inert.

## What this displaces

- **`kairix probe search` is NOT the production-latency gate.** Its
  measurement is the Python-pipeline regression surface — useful as a
  regression instrument (catches a change to the fusion logic that
  doubles single-query CPU time), not as a "is production fast enough"
  signal.
- **`kairix probe burst` is NOT the agent-experienced throughput gate.**
  Same reason — it runs as a cold CLI subprocess, not a warm MCP client.
  The probe's own first live run produced a 58.7 % qps drop that was
  mostly subprocess cold-start, not real degradation (filed as #283).
- **The architectural ADR [`teaming-concurrency-strategy.md`](teaming-concurrency-strategy.md)
  decisions stay valid.** The probe still surfaces the right Tier 1 lever
  (`pool_exhaustion_or_cache_miss` recommendation) even with the wrong
  measurement shape; it just over-reports the magnitude. The hypothesis-
  driven tuning approach is unchanged.

## What this preserves

- All existing test layers stay as they are. No deletions.
- The `kairix probe search/burst` CLIs stay shipped and operator-callable
  — they're the Python-pipeline regression gate, plus the PVT toolkit
  the future `MCPHttpSearchClient` plugs into.
- Existing BDD scenarios are not rewritten. They measure semantic
  correctness, not performance; that's still the right job.

## Acceptance — when this approach is re-opened

This decision should be revisited when ANY of:

1. CI duration creeps above 10 min consistently, forcing a triage of what
   gets kept in upfront vs deferred to PVT.
2. PVT becomes load-bearing for release decisions (e.g. release gate cuts
   over to a "PVT must pass on the canary deploy"). At that point PVT is
   gating production but operators are still in the loop — the line
   shifts from "PVT is opt-in" to "PVT is required for the canary stage".
3. A new test transport surface lands (e.g. gRPC, WebTransport) that the
   four-layer taxonomy doesn't model.
4. The in-process probe surfaces evidence contradicting the load-shape
   analysis in `teaming-concurrency-strategy.md` such that the "Python-
   pipeline regression" framing no longer fits.

## Related

- [`teaming-concurrency-strategy.md`](teaming-concurrency-strategy.md) — the
  decision the probes were built to support.
- [`operational-tests-design.md`](operational-tests-design.md) — the
  three-phase rollout of soak / probe / capabilities introspection.
- [`go-integration-plan.md`](go-integration-plan.md) — adjacent decision on
  language scope; same explicit-layering pattern.
- [#283](https://github.com/three-cubes/kairix/issues/283) — probe burst
  warmup-detection fix (the methodology gap that surfaced this redefinition).
- [#284](https://github.com/three-cubes/kairix/issues/284) — PVT harness
  build (the missing infrastructure behind the layer-4 scenarios).
