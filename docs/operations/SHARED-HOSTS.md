# Shared-host deployments

How to run kairix on a host that also runs other services without the worker starving its neighbours. This is the operator-facing companion to issue #224.

Use this guide when kairix is co-located with:

- a chat or agent runtime that has its own latency budget;
- another ingest pipeline;
- a graph database or other memory-hungry workload;
- anything where a sustained CPU burst from the kairix worker would be noticed.

For dedicated-host deployments you can stay on the default `docker-compose.yml` — the looser caps there are fine when nothing else competes for the host.

---

## Recommended headroom

Kairix is two workloads stacked. The serving path (`kairix` container, `/mcp` endpoint, search) is latency-sensitive and lightly loaded between requests. The background worker (`kairix-worker`) does scans, embeds, and entity maintenance — it bursts hard during work, and should sit at ~0 CPU when idle (after #224 phase 1's idle backoff lands).

For a single shared host running kairix + neo4j + one or two co-located agents, plan for:

| Resource | Minimum | Comfortable | Notes |
|---|---|---|---|
| CPU | 2 vCPU | 4 vCPU | Worker bursts are short but heavy. 2 vCPU works if you accept slow embed cycles; 4 vCPU lets the worker finish a backfill in a reasonable window. |
| Memory | 4 GiB | 8 GiB | kairix ~1.2 GiB steady, neo4j ~1-2 GiB, worker ~256 MiB idle / ~1 GiB during embed. Leave 1-2 GiB for the host kernel and other tenants. |
| Disk | 10 GiB | 20+ GiB | The usearch index and SQLite content store grow with your document count; plan ~1 GiB per 50k chunks. |

If the host is smaller than the minimum row, kairix will run but you will see exactly the symptoms #224 describes: event-loop starvation in co-located services, sporadic request timeouts, and restart churn. Stop the worker before stopping anything else — see "Pausing the worker safely" below.

---

## The worker is the noisy neighbour

The serving path is well-behaved. The worker is the one that needs explicit caps.

What the worker does that costs CPU and I/O:

1. **Scan.** Walks the document store, hashes changed files. Cheap on small stores, expensive on large or networked filesystems.
2. **Embed.** Calls the embedding provider for new or changed chunks. Network-bound if you use a cloud provider, CPU-bound if you use a local model.
3. **Index load.** Loads the usearch ANN index into memory. Cost paid per worker generation; large indexes dominate RSS.
4. **Recall and entity maintenance.** Recall canary, entity seeding, graph maintenance. Runs even when no embedding work happened (the bug #224 phase 1-2 is fixing).
5. **Repeat.** Loops on its scan interval.

Until #224 phase 1's idle backoff lands, the loop spins even on a no-op cycle, and steps 4 and 5 are where the worker becomes the noisy neighbour. With idle backoff in place, the worker stays at ~0 CPU between real work; without it, the only safe operating mode on a constrained host is to pause the worker when you don't need new content indexed.

Bounded resources are the second line of defence. See `docker-compose.example.yml` at the repo root for `deploy.resources` blocks tuned for this scenario. The worker gets:

- A **low CPU reservation** (0.25 vCPU) so the scheduler doesn't strand CPU during idle.
- A **higher CPU limit** (1.0 vCPU) so legitimate embed bursts finish in a reasonable time.
- A **modest memory reservation** (256 MiB) matching idle RSS.
- A **firm memory limit** (1 GiB) that's enough for a typical embed cycle but not enough for a runaway maintenance run to take the host down.

If you see request timeouts on co-located services during a worker burst, tighten the worker's `cpus.limit` toward 0.5 vCPU. Embed cycles will get longer; the host will stop choking. That's usually the right trade on a shared host.

---

## Pausing the worker safely

The serving path stays healthy when the worker is stopped — this was confirmed during the #224 incident and is the basis of the "decouple serving from indexing" pattern.

Today, stop the worker container directly:

```bash
docker compose stop kairix-worker
```

Search, `/mcp` calls, and the graph database keep working. The index is read-only from the serving path's perspective — you just stop getting new content indexed until you start the worker again:

```bash
docker compose start kairix-worker
```

A first-class `kairix worker pause` / `kairix worker resume` interface lands with #224 phase 4. Once it's available it will pause the loop in-process without restarting the container, which avoids the index reload cost on resume. Until then, `docker compose stop` is the recommended pause.

---

## Restart-storm anti-pattern

The default `docker-compose.yml` uses `restart: unless-stopped` on every service. That's fine when failures are rare. On a constrained host it can mask a degraded worker: the worker starts, hits an out-of-memory condition or a config error, exits, and the runtime restarts it immediately. Each restart pays the full cold-start cost (index load, recall canary, entity seed). The host stays at sustained 100% CPU with no progress.

If you have ever seen "the worker is always busy but the index never moves", this is what you saw.

Mitigations, in order of preference:

1. **Use `restart: on-failure` with a max-attempts cap.** The example file ships with `restart: on-failure:5`. After five failed restarts the container stays down and the failure becomes visible instead of being papered over. Exec into the host and read `docker logs kairix-worker --tail 200` to see why it's failing.
2. **Surface restart count via `kairix worker status`** (lands with #224 phase 5). The status command will report restart count and last-failure reason, so a health probe or monitoring loop can detect churn even when `docker ps` shows the container as "running".
3. **For Compose Spec deployments (Swarm), use `restart_policy` with `max_attempts` and a sensible `window`.** Same intent, native to Compose Spec.

If the worker is failing repeatedly, do not raise its memory limit blindly. Read the logs first — the most common cause is a misconfigured `KAIRIX_DOCUMENT_ROOT` or a missing embed provider credential, neither of which more memory fixes.

---

## Putting it together

1. Read this page.
2. Copy `docker-compose.example.yml` from the repo root as your starting compose file (or layer it over `docker-compose.yml`).
3. Watch `docker stats` for a week. Tighten or loosen `deploy.resources` to match what you actually see.
4. Set `restart: on-failure:5` on the worker (the example file does this for you).
5. When you co-locate something latency-sensitive, run `docker compose stop kairix-worker` before any benchmark or load test. Confirm the latency issue tracks the worker; if it does, plan the worker's runtime around the latency-sensitive workload's quiet hours, or split it onto its own host.

See also:

- [`docker-compose.example.yml`](../../docker-compose.example.yml) — example compose with bounded `deploy.resources` blocks.
- [OPERATIONS.md](OPERATIONS.md) — deployment, configuration, secrets.
- [Issue #224](https://github.com/three-cubes/kairix/issues/224) — full requirements for shared-host worker discipline.
