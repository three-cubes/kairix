# How-to — Audit the Kairix entity graph

**When to use:** the entity graph is drifting from what the document store says — `kairix entity suggest` returns garbage, agents report "X not found" for entities you know exist, or the reflib benchmark regresses on entity-heavy queries. This runbook walks two purposeful commands: `kairix entity audit` (read-only) then `kairix entity purge` (destructive, dry-run gated).

**Time:** 5-15 minutes for detection + repair on a typical knowledge store (≤ 5k entity nodes). Add the crawl time if you escalate to a full rebuild.

**Warning:** Step 3 (safe purge) is destructive. It runs in dry-run mode by default and the runbook keeps it that way until the captured report has been reviewed. Never skip the dry-run.

**Replaces:** the retired Mnemosyne `entity-audit.py` practice and Kairix's earlier six-command stitched workflow that mixed `kairix curator health`, `kairix store health`, and `scripts/prune-entities.py` outputs. The new surface is two commands sharing a single report shape.

---

## Configure for your environment

| Reference | Substitute with |
|---|---|
| `${KAIRIX_DOCUMENT_ROOT}` | `kairix.config.yaml` `paths.document_root`, or `KAIRIX_DOCUMENT_ROOT` env var |
| `${KAIRIX_NEO4J_URI}` | `bolt://<host>:7687`, set in your env or secrets |
| `${KAIRIX_NEO4J_USER}` | typically `neo4j` |
| `${KAIRIX_NEO4J_PASSWORD}` | from your secrets pipeline |
| `${KAIRIX_DATA_DIR}` | `kairix.config.yaml` `paths.db_path` parent, or env var (default `~/.cache/kairix/`) |

If kairix runs in Docker, prefix host commands with `docker exec <container-name>`.

## When to use this runbook

Reach for this runbook when one or more of the following shows up:

- `kairix entity suggest "<text>"` returns clear false positives — role phrases, document filenames, or fragments that aren't real entities.
- An agent that previously located an entity now reports it missing, but the source note is still in `${KAIRIX_DOCUMENT_ROOT}`.
- `kairix benchmark run --suite reflib` shows recall regression on entity-anchored queries (NDCG@10 drop on category `entity`).
- `kairix curator health` reports a non-zero `issue_count` — synthesis failures, stale entities, or missing `vault_path` properties.
- After a document-store reorg (file moves, folder renames, mass deletions) you suspect orphan entity nodes.

If the symptom is "entity-enriched search shows zero entities for everything," skip this runbook and go to [how-to-rebuild-entity-graph](how-to-rebuild-entity-graph.md) — that's a state problem, not an audit problem.

## Step 0 — Capture pre-audit state

Snapshot the graph before you touch it so you can compare after.

```bash
# Overall onboarding health — Neo4j reachable? embed pipeline running?
kairix onboard check --json > /tmp/kairix-onboard-pre.json
jq '{ok, neo4j, vector_index, document_root}' /tmp/kairix-onboard-pre.json

# Entity counts by type
kairix store health --json > /tmp/kairix-store-pre.json
jq '{ok, total_entities, entities_by_type, errors}' /tmp/kairix-store-pre.json
```

Keep both files. The post-audit step compares against this baseline.

If `kairix onboard check` reports Neo4j unreachable, stop. Resolve connectivity first — every step below queries Neo4j.

## Step 1 — Run the one-shot audit (read-only)

`kairix entity audit` covers three lenses in one command:

- `junk` — entities with no `vault_path` AND no `summary` (never enriched, never anchored to a stub).
- `paths` — entities whose `vault_path` no longer exists on disk (source note was renamed, moved, or deleted).
- `enrichment` — entities missing `summary`, `wikidata_qid`, or label (incomplete enrichment).
- `all` (default) — the deduplicated union.

```bash
# Quick look — text report to stdout
kairix entity audit --mode all

# Capture the JSON report — this is what `kairix entity purge` consumes
kairix entity audit --mode all --format json --output /tmp/audit.json

# Inspect the report shape
jq '{mode, generated_at, total, rows: .rows[:3]}' /tmp/audit.json
```

The JSON shape is:

```json
{
  "mode": "all",
  "generated_at": "2026-05-14T00:00:00Z",
  "total": 12,
  "rows": [
    {
      "id": "ghost-1",
      "name": "Ghost One",
      "type": "Concept",
      "mode": "junk",
      "reason": "no vault_path and no summary"
    }
  ]
}
```

Every row carries the `mode` lens that flagged it so you can filter the report before purging. Useful slices:

```bash
# Only junk candidates
jq '.rows[] | select(.mode == "junk")' /tmp/audit.json

# Only path candidates — these are the safest to delete
jq '.rows[] | select(.mode == "paths") | {name, reason}' /tmp/audit.json

# Only enrichment gaps — these usually want repair, not delete
jq '.rows[] | select(.mode == "enrichment")' /tmp/audit.json
```

**Remediation when the report is small (< 20 rows):** prefer manual cleanup. For `enrichment` rows, run `kairix entity validate "<Name>" --update` to add `wikidata_qid` where Wikidata has a confident match. For `junk` and `paths` rows you've confirmed by hand, edit the audit JSON to keep only those rows and proceed to Step 2.

**Remediation when the report is large (≥ 20 rows):** filter first. The most common pattern is "purge `paths` rows in batch, then rerun audit for the remaining junk/enrichment work":

```bash
# Filter to paths-only rows
jq '. + {rows: [.rows[] | select(.mode == "paths")]} | .total = (.rows | length)' \
   /tmp/audit.json > /tmp/audit-paths-only.json
```

## Step 2 — Review the audit JSON

Before purging, walk the rows by name. The audit report records *what* and *why*; you confirm *yes, delete*. For any row you're unsure about, look at the source document store path:

```bash
# Spot-check one row from the paths lens
jq -r '.rows[] | select(.mode == "paths") | .reason' /tmp/audit.json | head -3

# Confirm the file is really gone
test -f "${KAIRIX_DOCUMENT_ROOT}/<vault_path-from-reason>" \
  && echo "STILL EXISTS — remove this row from the audit before purge" \
  || echo "missing — purge candidate confirmed"
```

If you find rows that should not be purged, edit `/tmp/audit.json` to remove them. The purge command consumes the file verbatim.

## Step 3 — Purge with `kairix entity purge`

`kairix entity purge` requires either `--dry-run` or `--execute`. There is no default — you always declare intent.

```bash
# Always preview first
kairix entity purge --audit-report /tmp/audit.json --dry-run
```

The dry-run prints the Cypher template (`MATCH (n {id: $id}) DETACH DELETE n`) and every candidate row. No Cypher actually runs. When the preview looks right:

```bash
# Apply, capturing the audit log
kairix entity purge --audit-report /tmp/audit.json --execute \
  2>&1 | tee /tmp/kairix-purge-execute.log
```

The execute log records, per row: `id`, `name`, `type`, `mode`, `reason`, and `status` (one of `deleted`, `skipped: no id`, or `error: <Class>: <msg>`). The summary line shows `Deleted: <n> / <total>`.

Keep `/tmp/kairix-purge-execute.log` with your operator records — it's the only retrievable record of which nodes were removed and why.

**JSON output for automation:**

```bash
kairix entity purge --audit-report /tmp/audit.json --execute --format json \
  > /tmp/kairix-purge.json
jq '{dry_run, deleted_count, candidate_count, errors: [.audit_log[] | select(.status | startswith("error"))]}' \
   /tmp/kairix-purge.json
```

**Atomicity:** each `DETACH DELETE n` is one Cypher statement, atomic at the Neo4j level. If the executor crashes mid-run, previously-deleted nodes stay deleted and the remaining list is unaffected. Re-running `audit` regenerates the report against the live graph; pointing the next purge at the new file picks up the work.

**Rollback:** Neo4j has no built-in undo for `DETACH DELETE`. If you need to roll back, the recovery path is a full graph rebuild from the document store — see [how-to-rebuild-entity-graph](how-to-rebuild-entity-graph.md). Snapshot Neo4j before any large `--execute` run if your deployment supports it.

## Step 4 — Validate post-audit

Re-run the baseline captures from Step 0 and diff:

```bash
# Capture the post-audit state
kairix onboard check --json > /tmp/kairix-onboard-post.json
kairix store health --json > /tmp/kairix-store-post.json
kairix entity audit --mode all --format json --output /tmp/audit-post.json

# Compare entity totals — total should drop by exactly the purge count
jq '.total_entities' /tmp/kairix-store-pre.json /tmp/kairix-store-post.json

# Compare audit row counts — should drop, never rise
jq '.total' /tmp/audit.json /tmp/audit-post.json

# Recall regression check — reflib benchmark on the live system
kairix benchmark run --suite reflib --system hybrid \
  --output "${KAIRIX_DATA_DIR}/benchmark/post-entity-audit"
```

Compare the post-audit reflib NDCG@10 against your last green run. If recall regressed, the purge took out a legitimate entity. Recovery path: [how-to-rebuild-entity-graph](how-to-rebuild-entity-graph.md) restores from the document store.

## Step 5 — Override coverage (optional, when overrides are in use)

The vault-driven override loader at `${KAIRIX_DOCUMENT_ROOT}/04-Agent-Knowledge/_entity-overrides.md` lets curators force-add or force-block entity terms. An override that's allowlisted but never matches in a crawl is dead weight.

```bash
# List every allowlisted name in the overrides file
grep -E "^- " "${KAIRIX_DOCUMENT_ROOT}/04-Agent-Knowledge/_entity-overrides.md" \
  | sed 's/^- //' \
  | sort -u > /tmp/kairix-overrides-names.txt

# For each name, check whether it exists as a Neo4j entity
while read -r name; do
  result=$(kairix entity get "$name" --format json 2>/dev/null | jq -r '.id // "missing"')
  printf "%-40s %s\n" "$name" "$result"
done < /tmp/kairix-overrides-names.txt
```

Entries with `missing` are either a new override the next crawl will pick up (run `kairix store crawl` and re-check), a drift-prone name (canonical form changed — edit the override), or genuinely unused (remove the line).

## Escalation — graph state is too broken to repair

When the audit report shows > 50% of entities flagged, or repeated audit+purge cycles surface different sets of orphans, the underlying problem is graph state, not audit. Stop running purge cycles and rebuild from scratch — see [how-to-rebuild-entity-graph](how-to-rebuild-entity-graph.md).

## Gaps and follow-up work

| Wanted surface | Status |
|---|---|
| `kairix entity audit` (one-shot audit) | landed (#260) |
| `kairix entity purge --dry-run / --execute` | landed (#261) |
| `kairix entity count` (just the number) | `kairix store health --json \| jq '.total_entities'` |
| `kairix store crawl --reset` (drop-and-rebuild in one command) | manual `MATCH (n) DETACH DELETE n` + `kairix store crawl` |
| Override-coverage stats (matched N times in last crawl) | shell loop over `_entity-overrides.md` + `kairix entity get` |

The retired `scripts/prune-entities.py` is kept as a deprecated shim for now and will be removed in a future release. New automation should call `kairix entity audit` + `kairix entity purge`.

## See also

- [how-to-rebuild-entity-graph](how-to-rebuild-entity-graph.md) — full rebuild when audit + purge isn't enough.
- [entity-overrides user guide](../../user-guide/entity-overrides.md) — vault-driven allowlist/blocklist.
- `kairix entity audit --help` — flag reference for the audit surface.
- `kairix entity purge --help` — flag reference for the purge surface.
