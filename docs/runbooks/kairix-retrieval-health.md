# Runbook — Kairix retrieval health and recovery

**Severity:** P1 — retrieval degraded; agents may surface wrong, partial, or empty answers.

You are the operator (human or agent) diagnosing a kairix deployment whose retrieval surface has gone bad. This runbook is the playbook for finding which subsystem broke and bringing it back, in dependency order. Every step ends with a concrete next action — if a step does not give you one, stop and escalate.

Closes [#255](https://github.com/three-cubes/kairix/issues/255).

---

## 1. When to use this runbook

Reach for this runbook when any of the following surface:

- A dogfood report says "search is returning the wrong stuff" or "search returned nothing for a query that should hit."
- The recall canary suite regressed below its committed baseline (see §5).
- An agent surfaces "kairix returned 0 results" or "vec=0, vec_failed=true" in its envelope.
- A scheduled health check shows `kairix onboard check` exit code 1 against a deployment that was previously green.
- A new release dropped and you want to confirm retrieval still works before agents start their next session.

Do NOT use this runbook for:

- Secrets rotation — link forward to `kairix-secrets-rotation.md` (the next runbook the operator owes; tracked alongside this one).
- MCP transport failures (`-32602 Invalid request parameters`) — see [`MCP-CLIENT-MIGRATION.md`](../operations/MCP-CLIENT-MIGRATION.md).
- Per-query ranking issues where most results are fine but one query scores low — see [`how-to-debug-search-ranking.md`](../operations/runbooks/how-to-debug-search-ranking.md) in the operations runbooks.

---

## 2. First diagnostic — `kairix onboard check --json`

Always start here. The check runs nine independent subsystem probes in dependency order (PATH → secrets → document root → vector search → Neo4j → agent knowledge → chunk_date → MCP service) and emits a structured envelope you can act on without further parsing.

```bash
kairix onboard check --json
```

Expected envelope shape:

```json
{
  "passed": 9,
  "total": 9,
  "fully_passed": true,
  "failures": [],
  "env_source": "/run/secrets/kairix.env"
}
```

Failure envelope:

```json
{
  "passed": 6,
  "total": 9,
  "fully_passed": false,
  "failures": [
    {
      "check": "vector_search_working",
      "detail": "Vector search failed (vec_failed=True). Results: 12 (BM25 only). bm25=12, vec=0",
      "remediation": "Run `docker logs kairix-worker-1` for embed-pipeline errors; confirm `kairix onboard check secrets_loaded` passes; then run `kairix embed --limit 20` to test the embed pipeline."
    }
  ],
  "env_source": "/run/secrets/kairix.env"
}
```

Read the `failures` array top-down — checks are emitted in dependency order, so the first failure usually explains the rest. Each failure carries a `remediation` string that is your next action; if you take that action and the failure persists, branch into the matching section below.

**Next action:** pipe the JSON into `jq '.failures[] | {check, remediation}'` and read the first row. Branch on `check` using §3.

---

## 3. Diagnosis tree — branch on the failed check

### `secrets_loaded: false`

Credentials for the embed/LLM provider are missing or not loaded into the kairix process environment. Vector search will fail (Azure embed calls error out), the worker will spin without progress, and `kairix bootstrap` will surface a degraded envelope.

**Next action:** stop here and follow the `kairix-secrets-rotation.md` runbook (the next runbook the operator owes; live alongside this one in `docs/runbooks/`). Do NOT try to fix vector search until secrets are present — every downstream check fails for the same root cause.

Interim sanity check while waiting for that runbook to ship:

```bash
# Confirm the secrets file exists and has both required keys.
grep -E '^(KAIRIX_LLM_API_KEY|KAIRIX_LLM_ENDPOINT)=' /run/secrets/kairix.env
# Expected: two lines, one per key, non-empty values.
```

### `vector_search_working: false`

The hybrid search pipeline ran but the vector leg returned zero or raised. The BM25 leg may still be working, so search is degraded (keyword-only) rather than dead.

Three sub-causes, in order of likelihood:

1. **Embed credentials are present but invalid or rate-limited.** Confirm `secrets_loaded` passed first; if it did, the credentials reached the process but the provider rejected them. Run:

   ```bash
   kairix embed --limit 1
   # Look for HTTP 401 (auth), HTTP 429 (rate limit), or connection error.
   ```

   - HTTP 401 → rotate via `kairix-secrets-rotation.md`.
   - HTTP 429 → reduce `--batch-size` (default 250) or slow the embed schedule.
   - Connection error → check network reach from the kairix host to `KAIRIX_LLM_ENDPOINT`.

2. **Vectors were never embedded.** No chunks in `content_vectors`. Run:

   ```bash
   kairix embed status
   # Reports embedded chunk count, last run timestamp.
   ```

   If embedded count is zero, run a full embed (see §4 — Per-failure recovery, "No vectors indexed yet").

3. **Vector index file is missing or corrupt.** See [`runbook-vector-search-failure.md`](../operations/runbooks/runbook-vector-search-failure.md) — the deep dive for this branch lives there.

**Next action:** run the `kairix embed --limit 1` probe above. Branch on the error class.

### BM25 search is failing (FTS table missing)

`onboard check` does NOT have a standalone `bm25_search_working` check today (tracked as a follow-up below), so this branch is detected indirectly: `vector_search_working` reports BM25 count = 0 alongside vec_failed=false, OR the worker log shows "no such table: documents_fts". The FTS5 table dropped or was never built.

```bash
# Confirm the FTS table state.
kairix embed rebuild-fts
# Expected: "FTS state before rebuild: available=False reason=missing rows=0"
#           "FTS state after rebuild:  available=True reason=ok rows=N"
#           "Rebuilt: N documents indexed"
```

`rebuild-fts` is a self-heal subcommand (#223) that drops and atomically rebuilds `documents_fts` without touching the embed pipeline, vector index, or recall canaries. Cheap (~30s on a 50k-doc corpus).

**Next action:** run `kairix embed rebuild-fts`, then re-run `kairix onboard check --json` to confirm green.

### `agent_knowledge_populated: false`

Document store has no `04-Agent-Knowledge/<agent>/memory/*.md` files. Briefing and recent-context synthesis will return empty.

Two sub-causes:

1. **The agent has not written any memory yet** — expected for a freshly-onboarded agent. Run one agent session and re-check.
2. **The document crawler / sync wiped the directory.** Confirm the path exists and has files:

   ```bash
   ls "${KAIRIX_DOCUMENT_ROOT}/04-Agent-Knowledge/"
   # Expected: one directory per active agent.
   ```

   If empty, your sync mechanism (Obsidian Sync, git pull, rsync) is out of date. Re-sync and re-run `kairix onboard check`.

**Next action:** confirm `${KAIRIX_DOCUMENT_ROOT}/04-Agent-Knowledge/` has expected agent directories. If yes but no memory files, run an agent session. If no agent directories, re-sync the document store.

### `chunk_date_populated: false`

`content_vectors.chunk_date` is missing or below 20% coverage. The TMP-7B temporal boost goes inert without it (recency ranking degrades), but search still returns results.

```bash
# Force re-embed to populate chunk_date from frontmatter and filenames.
kairix embed
# Expected: "Scanned documents: N new, M updated, K unchanged"
#           "Embedded N chunks, failed 0"
```

If chunk_date is still 0% after a full embed, your documents do not carry `date: YYYY-MM-DD` frontmatter and do not have dates in filenames. Add frontmatter to documents you want ranked temporally, then re-embed.

**Next action:** run `kairix embed`. If still 0%, add date frontmatter to a sample of documents and re-embed those.

### `neo4j_reachable: false`

Entity graph offline. Entity boost and multi-hop queries are degraded; vector + BM25 search still works.

```bash
# Confirm Neo4j is up.
sudo systemctl status neo4j
# Or for Docker: docker ps | grep neo4j

# If Neo4j is up but empty, repopulate from the document store.
kairix store crawl --document-root "${KAIRIX_DOCUMENT_ROOT}"
# Expected: "Document store crawl complete: <root>" with per-label counts.
```

This is graceful-degrade territory — kairix degrades cleanly when Neo4j is offline. Treat this as P2 unless dogfood reports specifically flag entity-related queries failing.

**Next action:** run `kairix store crawl` and confirm node count > 50 via `kairix onboard check`.

### Multiple failures

If three or more checks fail at once, you almost certainly have a foundational problem — secrets dropped, document root moved, or the kairix process is running with a stale environment. Skip individual fixes and jump to §6 (Full reset).

---

## 4. Per-failure recovery — exact commands

### No vectors indexed yet

```bash
# Run the embed pipeline against the configured document root.
kairix embed

# Watch progress.
kairix embed status
# Expected: "embedded N chunks, failed 0, duration Xs"

# Smoke test that vectors landed.
kairix search "topic definitely in your corpus" --json | jq '{vec_count, vec_failed}'
# Expected: vec_count > 0, vec_failed: false
```

**Validation:** `kairix onboard check --json | jq '.failures[].check'` shows no `vector_search_working` failure.

### Embed pipeline failing mid-run

```bash
# Pause the worker so a half-failed embed doesn't keep retrying.
kairix worker pause

# Inspect the worker state file.
kairix worker status
# Reports phase, counters, last error.

# Run a small embed manually to see the live error.
kairix embed --limit 20

# Once the error is fixed, resume.
kairix worker resume
```

**Validation:** `kairix worker status` shows phase=embedding or phase=idle, and `kairix onboard check` returns green.

### FTS table missing or corrupt

```bash
kairix embed rebuild-fts
```

**Validation:** the command's output shows `available=True reason=ok rows=N` after rebuild. Confirm with `kairix search "any keyword in your corpus"` returning results with `bm25_count > 0`.

### Entity graph stale or empty

```bash
kairix store crawl --document-root "${KAIRIX_DOCUMENT_ROOT}"
```

**Validation:** `kairix onboard check` shows `neo4j_reachable: ✓ Neo4j reachable — N nodes in graph` with N matching the document count.

### Recall canary suite is stale

```bash
# Re-sample canaries from the live corpus.
kairix embed --rebuild-canaries
```

Use after a major index rebuild — the persisted canary suite gets discarded and a fresh sample is drawn from the current corpus.

**Validation:** the embed run completes with `recall_score` printed and above the 0.85 floor.

---

## 5. Soak — does retrieval hold together under repeated load?

Symptom branch for "the gate passes but agents report degradation". Run a soak test — repeat the workload N times and assert no degradation across iterations.

```bash
kairix soak run --suite reflib --repeat 3 --json
```

Assertions (any failure exits 1, with a structured envelope in the JSON output):
- per-iteration RSS growth < 50 MB
- per-iteration wall time within 20% of iteration 0 (skipped on sub-100ms baselines)
- total stderr volume < 5 MB × repeat (catches the warning-spam regression class)
- no new file descriptors held at exit
- byte-identical `BenchmarkResult` signature across iterations (catches non-determinism)

If `kairix benchmark run` passes once but `kairix soak run --repeat 2` fails:
- **log_volume** failure → a per-call code path is spamming stderr. Common cause: deprecation warning fired on every call instead of once per process. Check the warning's surrounding code for missing dedup.
- **memory_growth** → an O(N) cache is growing without bound, or a closure is holding a reference past its iteration.
- **signature_mismatch** → the workload isn't deterministic. Look for clock-derived ordering, random sampling, or non-deterministic map iteration.
- **fd_leak** → a file/socket isn't being closed across iterations. Often a temp-file or HTTP client that's not in a `with` block.

`kairix soak run` is the operational complement to `kairix benchmark run` — same workload, but the assertion target is *system health*, not retrieval quality.

**MCP**: `tool_soak_run` returns an `OperatorOnlyCapability` envelope (soak is a multi-minute load test; agents must escalate). The envelope carries the exact `kairix soak run` command for the operator to invoke.

---

## 6. Concurrent-load probe — does p95 hold under teaming load?

Symptom branch for "individual queries feel fine but the team session goes flaky around 5+ active agents". The probe is the decision instrument for the Tier 1 tuning levers (Azure embed pool, query-result LRU cache, connection-pool sizes) laid out in [`docs/architecture/teaming-concurrency-strategy.md`](../architecture/teaming-concurrency-strategy.md) — run it *before* you commit to a tuning change so you pull the right lever, not the loudest one.

```bash
kairix probe search --suite reflib --queries 100 --concurrency 5 --recommend --json | jq .
```

Read three fields, in order: `overall.p95_ms` (gate is ≤ 500 ms — matches the ADR's agent-perceived-performance target; above it agents commit "kairix is flaky" to memory), `mean_concurrency` (Little's-Law `sum(durations)/wallclock`; approaches `--concurrency` when work overlaps, far below requested means a hidden lock, not a load problem), and `bottleneck.kind` / `bottleneck.recommended_action` (populated by `--recommend`; names the suspect subsystem and the lever).

| Observable signal | Suspected bottleneck | Next action |
|---|---|---|
| p95 climbs sharply at concurrency 2-5 | Azure embed pool exhausted | Tune `KAIRIX_EMBED_POOL_SIZE` + retry/backoff in `kairix/_azure.py` |
| p95 stays flat until concurrency 10-15 | Pool sizing fine; repeated-query overhead dominates | Add query-result LRU cache (Tier 1 lever 2) |
| `mean_concurrency` far below requested | Hidden lock contention — tasks serialised in-process | Investigate with `py-spy dump` against the live MCP process *before* pulling any lever |
| `bottleneck.kind == "azure_embed_rate_limit"` (429s) | Azure embed rate limit hit | Tune `KAIRIX_EMBED_POOL_SIZE` + backoff; do not raise pool past the Azure quota |
| `bottleneck.kind == "deployment_or_network"` (p95 high at concurrency=1) | Not a load problem | Check Azure endpoint health, cold-start latency, vault size |

On a fresh deployment or after a tuning change, sweep first rather than fixing a single concurrency — this is the recommended first run:

```bash
kairix probe search --suite reflib --queries 100 --concurrency-sweep 1,2,5,10,20 --recommend --json | jq .
```

The inflection where p95 starts to climb is the operating headroom; the level *at* the climb names the lever (≤5 → Azure pool; 10-15 → query cache). Pass/fail thresholds: `p95_ms` ≤ 500 ms (ADR gate), `p99_ms` ≤ 2000 ms, zero errors (any non-zero invalidates the reading). Agents can call `tool_probe_search` MCP for healthcheck-shaped probes up to `queries ≤ 20` / `concurrency ≤ 3`; above-cap calls return an `OperatorOnlyCapability` envelope with the exact CLI pre-filled — full load runs are operator-only by design.

```
fix: identify the bottleneck — re-run with --recommend, then apply the lever named in bottleneck.recommended_action
next: if recommendation is `worker_contention`, run py-spy against the live MCP process before pulling any tuning lever — the symptom is upstream of the levers
run: kairix probe search --suite reflib --queries 100 --concurrency 5 --recommend --json | jq .
```

---

## 7. Recall canary regression

This is a distinct symptom branch — your subsystem health checks pass, but the recall benchmark has dropped. Search is "working" in the sense that all probes pass; it's just returning worse results than it used to.

### Run the canary suite

```bash
kairix benchmark run --suite reflib
# Expected: per-category scores + a final weighted_total line.
```

### Compare against the committed baseline

The bundled reflib suite has a committed contract baseline at `benchmark-results/contract-baseline.json`. Use `compare` to diff a fresh run against it:

```bash
# Run with --output to write a result JSON.
kairix benchmark run --suite reflib --output benchmark-results/

# Compare against the committed baseline.
kairix benchmark compare benchmark-results/contract-baseline.json benchmark-results/<your-fresh-run>.json
```

The historical contract baseline carries `weighted_total: 0.9585` and `ndcg_at_10: 0.9466`. Local deployments will sit lower — the bundled reflib suite is for kairix's reference library and may not align with your corpus.

### Escalation thresholds

- **weighted_total drops below 0.85** — investigate. Run §3 diagnosis tree top-down even if every check is green; this usually means a corpus change (large bulk delete, schema rename) silently degraded ranking.
- **weighted_total drops more than 0.05 from your last known-good run** — regression; bisect the most recent config or index change. See [`runbook-benchmark-regression.md`](../operations/runbooks/runbook-benchmark-regression.md) for the bisect workflow.
- **A single category drops below 0.50 while others hold** — a specific intent dispatch path broke. See [`how-to-debug-search-ranking.md`](../operations/runbooks/how-to-debug-search-ranking.md).

**Next action:** if weighted_total < 0.85, file an issue with the run JSON attached and proceed to §6.

---

## 8. Full reset — last resort

Use this when individual fixes don't work, when three or more `onboard check` failures arrive at once, or after a botched migration. The full reset rebuilds every retrieval surface from the document store.

This is destructive of derived state (vectors, FTS, entity graph), NOT of source documents. Your document store is untouched.

```bash
# 1. Pause the worker so it doesn't fight you.
kairix worker pause

# 2. Re-embed from scratch (clears existing vectors).
kairix embed --force

# 3. Rebuild the BM25 / FTS table.
kairix embed rebuild-fts

# 4. Re-crawl the entity graph.
kairix store crawl --document-root "${KAIRIX_DOCUMENT_ROOT}"

# 5. Re-sample recall canaries from the rebuilt index.
kairix embed --rebuild-canaries

# 6. Resume the worker.
kairix worker resume

# 7. Confirm every subsystem green.
kairix onboard check --json | jq '{passed, total, fully_passed}'
# Expected: {"passed": 9, "total": 9, "fully_passed": true}

# 8. Confirm recall canary is at or above 0.85.
kairix benchmark run --suite reflib | tail -20
```

**Time estimate:** 20-60 minutes on a 50k-document corpus, dominated by step 2 (full re-embed). Step 3 (FTS rebuild) is ~30s. Step 4 (graph crawl) is ~5min.

**Next action:** once all eight commands above complete and the validation envelope is green, run one real agent session and confirm the dogfood symptom is resolved.

---

## 9. Escalation

File an issue at https://github.com/three-cubes/kairix/issues with the title `retrieval health: <symptom>` when:

- Step 6 (Full reset) does not resolve the symptom.
- A check fails that doesn't appear in §3.
- The same symptom recurs within 24 hours of a clean recovery.

Attach to the issue:

```bash
# Full structured diagnostic envelope.
kairix onboard check --json

# Last 50 lines of worker logs — wherever your deployment ships them.
# Docker: docker logs kairix-worker-1 --tail 50
# Systemd: sudo journalctl -u kairix-worker --no-pager -n 50
# Bare metal: tail -50 ${KAIRIX_DATA_DIR}/logs/embed.log

# Worker phase and counters.
kairix worker status

# Recent benchmark run (if recall regression).
kairix benchmark run --suite reflib --output /tmp/
# Attach /tmp/<latest>.json
```

Tag the issue with whichever dogfood agent reported the symptom — that's the primary signal for whether the recovery was real.

**Next action:** open the issue with the artefacts above pasted in, then watch for triage.

---

## See also

- [`teaming-concurrency-strategy.md`](../architecture/teaming-concurrency-strategy.md) — ADR for the concurrency model and the Tier 1 tuning levers the probe (§6) selects between.
- `kairix probe search --help` — full CLI surface for the concurrent-load probe, including `--concurrency-sweep`, `--p95-threshold-ms`, `--seed`, and `--recommend`.
- [`runbook-vector-search-failure.md`](../operations/runbooks/runbook-vector-search-failure.md) — deep dive on `vec=0, vec_failed=True` (vector leg only).
- [`runbook-embedding-lag.md`](../operations/runbooks/runbook-embedding-lag.md) — new content not searchable after the expected embed cycle.
- [`runbook-benchmark-regression.md`](../operations/runbooks/runbook-benchmark-regression.md) — NDCG dropped after a config or index change.
- [`how-to-debug-search-ranking.md`](../operations/runbooks/how-to-debug-search-ranking.md) — specific queries score poorly; per-intent dispatch tuning.
- [`how-to-rebuild-entity-graph.md`](../operations/runbooks/how-to-rebuild-entity-graph.md) — Neo4j entity graph repopulation procedure.
- `kairix-secrets-rotation.md` — the next runbook the operator owes; covers `KAIRIX_LLM_API_KEY` / `KAIRIX_LLM_ENDPOINT` rotation and the kairix-fetch-secrets service. Will live in this same directory.
