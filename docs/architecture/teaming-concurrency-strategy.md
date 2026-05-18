# Concurrency strategy for teaming environments — Python-with-tuning, not Go rewrite

> **Status**: decided. This document records the architectural decision and
> the reasoning that backs it. Re-open if the measurement instrument
> (`kairix probe search`) surfaces evidence that contradicts the load-shape
> assumptions below.

## Context

The primary kairix use case is teaming environments: 5-10 agents active in a
session, peak ~20 during session-start storms, each making occasional
retrieval calls (1-5 QPS sustained, 10-20 QPS bursts). The
agent-perceived-performance threshold is **p95 < 500ms for `tool_search`,
p99 < 2s** — past that, agents commit "kairix is flaky" to their memory and
avoid the tool (the brittleness pattern we've already remediated at the
cold-start layer via #278).

The architectural question raised: given the Go scaffolding now exists in
the repo (alpha-deploy webhook, G1-G10 fitness rules, Go quality CI gate),
should we be looking at rewriting parts of the retrieval pipeline in Go for
teamed-concurrency performance, given Python's GIL?

We need to decide *before* building the probe so the probe's measurements
inform tuning, not language-rewrite speculation.

## Load-shape analysis — CPU-bound vs I/O-bound surfaces

Walking one `tool_search` call end-to-end (typical case, ~300ms total):

| stage | nature | typical ms | GIL? |
|---|---|---|---|
| Intent classify (regex over query) | Python CPU | <1 | held |
| BM25 search (SQLite FTS5) | C extension | 30-80 | **released** during SQL |
| Embed query (Azure HTTP) | Network I/O | 80-200 | **released** during socket |
| Vector search (usearch) | C extension | 5-15 | **released** during ANN |
| RRF / bm25_primary fusion | Python CPU | 1-3 | held |
| Entity boost (if any) | Python CPU + Neo4j C | 10-30 | mostly released |
| Result rendering (dict build) | Python CPU | 1-2 | held |

**Python CPU (GIL-held) work is ~5-10ms per query. The remaining ~290ms is
either C-extension work that releases the GIL, or network I/O.**

At concurrency=20 (peak teaming load), serial Python CPU work is
~5-10ms × 20 = 100-200ms of GIL-serialised work per second per core. One
Python core handles that without saturating. **The GIL is not the bottleneck
at our expected load shape.**

## What WILL bottleneck first (likely order)

1. **Azure embed HTTP client connection pool / rate limit** — default httpx
   pool is small; sustained 20+ concurrent embed calls exhaust it before any
   Python-side issue surfaces.
2. **SQLite reader contention** — WAL mode handles ~50 concurrent readers
   fine; we're nowhere near saturation at typical loads.
3. **Neo4j driver pool sizing** — default max_pool=100; not a bottleneck at
   our load.
4. **Python heap pressure under concurrency** — 20 concurrent calls each
   with ~5MB working set = ~100MB transient. Trivially handled.
5. **Factory cache contention** — `dict.get()` is atomic in CPython; no
   lock needed for the read path. Not a bottleneck.

## Decision

**Keep the retrieval pipeline in Python. Use thread-pool concurrency
(via `concurrent.futures.ThreadPoolExecutor`) for parallel agent calls.
Tune the actual bottlenecks (Azure pool, query-result caching) before
considering structural language changes.**

This is consistent with the existing Go-integration plan
([`go-integration-plan.md`](go-integration-plan.md)): Go is for operational
binaries (webhook handlers, deploy wrappers, log shippers), not for
retrieval/MCP/agents/eval/domain logic. The Go-side fitness rules (G1-G10)
specifically scope to operational binaries.

## Why Go is the wrong answer for the pipeline (rejected option)

- **The CPU surface is too small**: 5-10ms of Python per query is what
  rewriting would speed up. The dominant cost (250-280ms) is in C extensions
  or network I/O that Go can't speed up — they're already as fast as the
  underlying primitive allows.
- **The heavy lifting libraries don't have Go equivalents at parity**:
  SQLite FTS5 (cgo wrappers exist but mature; fine), usearch (Go binding
  exists), sentence-transformers (no Go equivalent — would require Python
  or a separate model-serving process), neo4j (Go driver exists), spaCy
  (no Go equivalent at parity).
- **A pipeline rewrite breaks the Go scope rule** in
  `go-integration-plan.md` ("Go is only for ops binaries. Anything that
  touches retrieval, agents, eval, MCP, or domain logic stays in Python").
  That rule was deliberate; nothing in the load-shape analysis warrants
  revisiting it.
- **Complexity cost is high**: split-language deployment, two release
  trains, harder local development, separate dependency vetting paths. The
  Go integration plan accepted this cost for ops binaries specifically
  because they ship outside the Python venv. Pipeline code doesn't have
  that constraint.

## Why "process pool of Python MCP workers" is deferred

The classic Python-scaling answer is gunicorn-style worker pools (N Python
processes forked from a master). It would parallelise the small Python-CPU
slice that thread pools can't (due to GIL).

But:
- Each worker has its own factory cache → warm-up cost × N at startup
- Each worker has its own connection pools → 5× the Azure embed pool limit
  for the same effective throughput (worse, not better, under rate
  limits)
- The Python-CPU slice we'd parallelise is 5-10ms/query — saving that with
  N=5 workers gets us 1-2ms per query saved. Marginal at our latency budget.

**Revisit if measurement shows Python CPU saturating a core.** Until then,
threads in one process is simpler and adequate.

## What we WILL do (priority order)

### Tier 1 — likely worth doing (measure first to confirm)

1. **Tune the provider embed HTTP client pool size + retry/backoff config** —
   typical first contention point under concurrent load. One config block
   per provider plugin under `kairix/providers/<name>/`. The probe will
   tell us if it's saturated by surfacing 429s or pool-wait timing.

2. **Add query-result LRU cache** (small, keyed on `(query, scope, agent)`,
   bounded by item count + max age). In teaming environments where multiple
   agents ask similar questions within minutes, this is the highest-leverage
   change — it converts duplicate work into cache hits. Bigger win than any
   language-change.

3. **Expose connection-pool sizes in `kairix.config.yaml`** — SQLite WAL
   reader cap, Neo4j driver `max_pool`, Azure embed pool. Operator can tune
   per-deployment without code changes.

### Tier 2 — worth considering if Tier 1 measurements show we still have headroom problems

4. **Process pool for MCP server** (gunicorn workers). Cost: warm × N, pool
   × N. Only if probe shows Python CPU is the actual bottleneck.

5. **Async refactor of the pipeline composition** (`SearchPipeline.search()`
   becomes `async def`). End-to-end asyncio gives us true non-blocking I/O
   without thread overhead, but it's a substantial refactor. The benefit is
   marginal unless we're hitting thread-pool exhaustion.

### Tier 3 — actively rejected unless evidence emerges

6. **Rewrite search pipeline in Go**. Wrong tool; saves ms not seconds;
   breaks the Go-only-for-ops scope rule. Don't.

## Decision-making instrument: `kairix probe search`

The probe is the architectural decision-making instrument. Before pulling
ANY Tier 1 lever, the probe must tell us which one matters. Specifically:

- **p95 climbs sharply at concurrency=2-5** → Azure embed pool exhausted →
  pull lever 1 first.
- **p95 stays flat until concurrency=10-15** → Pool sizing is fine →
  query-cache (lever 2) is the next win.
- **`mean_concurrency` << requested concurrency at any level** → Hidden
  lock or serialisation surfaced. Investigate specifically before committing
  to a Tier 1 lever; could indicate a different problem class.

The probe also surfaces Azure HTTP status codes (429, pool exhaustion)
directly in its output and prints a `--recommend` summary naming the
suspected bottleneck and the specific config knob to try. F21 affordance
applied to load-test results.

## Acceptance — when this decision is re-opened

This decision should be revisited when ANY of:

1. The probe shows Python-CPU work consuming >40% of a core under typical
   teaming load (suggests GIL contention beyond what tuning fixes).
2. We hit a scaling regime not predicted by the analysis above (e.g.
   sustained 50+ QPS, where the CPU/IO ratio shifts).
3. A specific kairix surface needs sub-100ms p99 latency (current target is
   500ms p95 / 2s p99; tighter targets might warrant rethinking the stack).
4. Azure embed becomes async-incompatible or rate-limited in a way that
   demands client-side connection multiplexing Python can't deliver.

None of these is likely on our current trajectory. The probe is the
measurement that would tell us.

## What ships next

1. `kairix probe search` (sequential + concurrent + sweep) — implementation
   tracked by #276 Phase 2. Includes Azure HTTP diagnostics and the
   `--recommend` lever-suggestion output.
2. After first probe-driven measurement on the live VM: file specific
   issues for whichever Tier 1 lever(s) the probe indicates. Don't
   pre-commit to fixes that the measurement doesn't justify.
3. If the probe surfaces evidence contradicting the load-shape analysis
   above, re-open this decision before pulling Tier 1 levers.

## Related

- [`go-integration-plan.md`](go-integration-plan.md) — the existing Go scope
  rule that this decision is consistent with.
- [`sre-worker-design.md`](sre-worker-design.md) — the SRE worker that will
  consume `kairix probe search` on its rotation.
- [`operational-tests-design.md`](operational-tests-design.md) — Phase 2 of
  the operational-tests design (where this probe ships).
- [#276](https://github.com/three-cubes/kairix/issues/276) — operational
  tests + perf suite umbrella.
- [#278](https://github.com/three-cubes/kairix/issues/278) — cold-start
  warm-up (the closed work this builds on).
- [#279](https://github.com/three-cubes/kairix/issues/279) — memory audit
  (the closed work this builds on).
