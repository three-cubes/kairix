# How-to — Audit the Kairix entity graph

**When to use:** the entity graph is drifting from what the document store says — `kairix entity suggest` returns garbage, agents report "X not found" for entities you know exist, or the reflib benchmark regresses on entity-heavy queries. This runbook walks four audit modes in order of safety: detect, repair-paths, check-enrichment, then (only after evidence) safe-purge.

**Time:** 5-15 minutes for detection + repair on a typical knowledge store (≤ 5k entity nodes). Add the crawl time if you escalate to a full rebuild.

**Warning:** Step 4 (safe purge) is destructive. It runs in dry-run mode by default and the runbook keeps it that way until the captured report has been reviewed. Never skip the dry-run.

**Replaces:** the retired Mnemosyne `entity-audit.py` practice. Kairix's surfaces are different — primarily `kairix entity count`, `kairix curator health`, `kairix store health`, and `scripts/prune-entities.py` — so the procedure is rebuilt from those, not ported.

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

# Entity counts by type (#259 — pure count surface)
kairix entity count --json > /tmp/kairix-entity-count-pre.json
jq '{total, by_type}' /tmp/kairix-entity-count-pre.json

# Store-level health for synthesis/error context (kept alongside the count)
kairix store health --json > /tmp/kairix-store-pre.json
jq '{ok, errors}' /tmp/kairix-store-pre.json

# Curator-level health: synthesis failures + stale + missing vault_path
kairix curator health --format json --output /tmp/kairix-curator-pre.json
jq '{ok, total_entities, issue_count: (.synthesis_failures|length + (.stale_entities|length) + (.missing_vault_path|length))}' /tmp/kairix-curator-pre.json
```

Keep all three files. Each later audit step compares against this baseline.

If `kairix onboard check` reports Neo4j unreachable, stop. Resolve connectivity first — every step below queries Neo4j.

## Step 1 — Junk-entity detection (read-only)

"Junk" entities are nodes that exist in Neo4j but reference nothing real: no source document, no enrichment, suspicious name.

Kairix exposes this view through `kairix curator health` — specifically its `missing_vault_path` and `synthesis_failures` lists. Both are non-destructive: the command only reads.

```bash
# Run the curator health check in text mode for human review
kairix curator health --format text

# Or write the structured report to a file for diff-ing
kairix curator health --format json --output /tmp/kairix-curator-now.json

# Surface only the junk-candidate lists
jq '{
  no_vault_path: .missing_vault_path | map({name, entity_type, entity_id}),
  no_summary:    .synthesis_failures | map({name, entity_type, entity_id})
}' /tmp/kairix-curator-now.json
```

What each list means:

- `missing_vault_path` — entity has no `vault_path` property, so no canonical document stub anchors it. Either the stub never got created or the property never got written. These are seed-only entities and the most common form of junk.
- `synthesis_failures` — entity has no `summary` property. Either enrichment never ran or it errored out. Some of these are real entities awaiting enrichment — do not purge until you've cross-checked.
- `stale_entities` — entity not touched in `--staleness-days N` (default 90). Use `--staleness-days 30` for tighter scrutiny on a high-churn store. Staleness alone is not a purge signal; combine with missing vault_path.

**Remediation when junk is small (< 20 nodes):** prefer manual cleanup. Open each `vault_path` candidate in the document store, decide whether it's a legitimate entity awaiting a stub or a noise node from a failed crawl. Add legitimate ones to `${KAIRIX_DOCUMENT_ROOT}/04-Agent-Knowledge/_entity-overrides.md`. Drop the rest in Step 4.

**Remediation when junk is large (≥ 20 nodes):** proceed to Step 2 (path repair) first — many "junk" nodes are actually entities whose source file moved or was renamed, not entities that should be deleted.

## Step 2 — Path repair (find entities whose source path is gone)

The crawler writes a `vault_path` property on every entity it creates. If the underlying file is later renamed, moved, or deleted, the property goes stale — the node still exists, but its source is gone.

`scripts/prune-entities.py` classifies this case as `file_missing` and is dry-run by default.

```bash
# Dry-run — read-only, prints to stdout
python scripts/prune-entities.py --vault-root "${KAIRIX_DOCUMENT_ROOT}"

# Capture the report for evidence
python scripts/prune-entities.py --vault-root "${KAIRIX_DOCUMENT_ROOT}" \
  > /tmp/kairix-prune-dryrun.txt

# Count just the file_missing candidates
grep -c "file_missing" /tmp/kairix-prune-dryrun.txt
```

The report has two sections: `NODES TO DELETE` (file_missing + no_stub_no_summary) and `NODES TO KEEP`. Read the delete list. Every `file_missing` row should correspond to a `vault_path` you can confirm is gone from `${KAIRIX_DOCUMENT_ROOT}` by hand:

```bash
# Spot-check one or two paths from the dry-run report
test -f "${KAIRIX_DOCUMENT_ROOT}/<vault_path-from-report>" \
  && echo "STILL EXISTS — do not purge" \
  || echo "missing — purge candidate confirmed"
```

If a `file_missing` entry corresponds to a file that *does* still exist, the script has read a different vault root than your live document store. Re-run with `--vault-root` set explicitly and re-check.

**Affordance:** when `file_missing` count > 0, the next concrete step is Step 4 (safe purge with `--execute`). Do not run `--execute` until you've completed Step 3 too — enrichment audit may surface entities that should be enriched, not deleted.

## Step 3 — Enrichment audit (missing label, summary, source attribution)

Kairix entities ship with a small expected fact set: a `name`, a Neo4j label (e.g. `Organisation`, `Person`, `Concept`), a `summary`, and a `vault_path`. Anything missing is an enrichment gap.

Two surfaces tell you what's enriched:

```bash
# Surface 1: curator health — synthesis_failures = no summary written
kairix curator health --format json --output /tmp/kairix-curator-enrich.json
jq '.synthesis_failures | map({name, entity_type, entity_id})' \
   /tmp/kairix-curator-enrich.json

# Surface 2: entity count — totals + by-type rollup (#259)
kairix entity count --json | jq '{total, by_type}'
```

For entities flagged in `synthesis_failures` that you want to keep, run validation against Wikidata:

```bash
# Per-entity validation — adds wikidata_qid if a high/medium-confidence match exists
kairix entity validate "<Name>" --update

# JSON form for automation
kairix entity validate "<Name>" --format json
```

For batch enrichment, re-run the crawler against the document store so the latest `vault_path` and any wikilink-derived attribution get written:

```bash
# Full crawl — picks up renamed files, new wikilinks, refreshes vault_path
kairix store crawl --document-root "${KAIRIX_DOCUMENT_ROOT}"
```

**Affordance:** an entity that's missing both `summary` and `vault_path` after a fresh crawl + a Wikidata validation pass is junk. Move it to the Step 4 purge list.

## Step 4 — Override coverage (allowlisted but never matched)

The vault-driven override loader at `${KAIRIX_DOCUMENT_ROOT}/04-Agent-Knowledge/_entity-overrides.md` (see [entity-overrides user guide](../../user-guide/entity-overrides.md)) lets curators force-add or force-block entity terms. An override that's allowlisted but never matches in a crawl is dead weight — the term is wrong, the spelling drifted, or the source content was removed.

Kairix does not today track "override matched N times" — see "Gaps" below. The practical audit:

```bash
# 1. List every allowlisted name in the overrides file
grep -E "^- " "${KAIRIX_DOCUMENT_ROOT}/04-Agent-Knowledge/_entity-overrides.md" \
  | sed 's/^- //' \
  | sort -u > /tmp/kairix-overrides-names.txt

# 2. For each name, check whether it exists as a Neo4j entity
while read -r name; do
  result=$(kairix entity get "$name" --format json 2>/dev/null | jq -r '.id // "missing"')
  printf "%-40s %s\n" "$name" "$result"
done < /tmp/kairix-overrides-names.txt
```

Entries with `missing` are either:
- A new override the next crawl will pick up (run `kairix store crawl` and re-check).
- A drift-prone name (the canonical form in the document store changed). Edit the override.
- Genuinely unused. Remove the line from `_entity-overrides.md`.

## Step 5 — Safe purge (destructive, dry-run gated)

Only run this step after Steps 1-4 have produced a delete list you've reviewed by name.

```bash
# Re-run the dry-run as your final check (capture the report you'll act on)
python scripts/prune-entities.py --vault-root "${KAIRIX_DOCUMENT_ROOT}" \
  > /tmp/kairix-prune-final-dryrun.txt

# Diff against the earlier dry-run to confirm nothing surprising changed
diff /tmp/kairix-prune-dryrun.txt /tmp/kairix-prune-final-dryrun.txt \
  || echo "delete list changed — investigate before --execute"

# Apply, capturing the audit log of every deletion
python scripts/prune-entities.py \
  --vault-root "${KAIRIX_DOCUMENT_ROOT}" \
  --execute \
  2>&1 | tee /tmp/kairix-prune-execute.log

# The log records: NAME, TYPE, REASON for every node deleted, plus the
# total deleted / total scanned summary.
```

The execute log is the audit trail. Keep it with your operator records — it's the only retrievable record of which nodes were removed and why.

**Atomicity:** each `DETACH DELETE n` is one Cypher statement, atomic at the Neo4j level. The script does not batch; if it crashes mid-run, previously-deleted nodes stay deleted and the remaining list is unaffected. Re-running the script picks up where it stopped because the dry-run report regenerates against the live graph.

**Rollback:** Neo4j has no built-in undo for `DETACH DELETE`. If you need to roll back, the recovery path is a full graph rebuild from the document store — see [how-to-rebuild-entity-graph](how-to-rebuild-entity-graph.md). Snapshot Neo4j before any large `--execute` run if your deployment supports it.

## Step 6 — Validate post-audit

Re-run the baseline captures from Step 0 and diff:

```bash
# Capture the post-audit state
kairix onboard check --json > /tmp/kairix-onboard-post.json
kairix entity count --json > /tmp/kairix-entity-count-post.json
kairix store health --json > /tmp/kairix-store-post.json
kairix curator health --format json --output /tmp/kairix-curator-post.json

# Compare entity totals — total should drop by exactly the prune count
jq '.total' /tmp/kairix-entity-count-pre.json /tmp/kairix-entity-count-post.json

# Compare issue counts — should drop, never rise
jq '{
  synth: (.synthesis_failures|length),
  stale: (.stale_entities|length),
  no_path: (.missing_vault_path|length)
}' /tmp/kairix-curator-pre.json
jq '{
  synth: (.synthesis_failures|length),
  stale: (.stale_entities|length),
  no_path: (.missing_vault_path|length)
}' /tmp/kairix-curator-post.json

# Recall regression check — reflib benchmark on the live system
kairix benchmark run --suite reflib --system hybrid \
  --output "${KAIRIX_DATA_DIR}/benchmark/post-entity-audit"
```

Compare the post-audit reflib NDCG@10 against your last green run. If recall regressed, the purge took out a legitimate entity. Recovery path: [how-to-rebuild-entity-graph](how-to-rebuild-entity-graph.md) restores from the document store.

## Escalation — graph state is too broken to repair

When the audit report shows > 50% of entities flagged as junk, or repeated path-repair runs surface different sets of orphans, the underlying problem is graph state, not audit. Stop running prune cycles and rebuild from scratch.

```bash
# Drop every entity and relationship — destructive
cypher-shell -a "${KAIRIX_NEO4J_URI}" -u "${KAIRIX_NEO4J_USER}" \
             -p "${KAIRIX_NEO4J_PASSWORD}" \
             "MATCH (n) DETACH DELETE n"

# Full crawl from the document store
kairix store crawl --document-root "${KAIRIX_DOCUMENT_ROOT}"

# Verify
kairix store health
kairix curator health --format text
```

The full sequence — including pre-checks, content checks, and post-rebuild verification — is documented in [how-to-rebuild-entity-graph](how-to-rebuild-entity-graph.md). Use that runbook for any rebuild, not the three commands above.

## Gaps and follow-up work

Some surfaces this runbook would benefit from do not exist today. Where the runbook calls a primitive that's missing, it routes through the closest available command. These gaps are tracked as follow-up issues:

| Wanted surface | Closest substitute today | Gap issue |
|---|---|---|
| `kairix entity audit` (one-shot audit covering Steps 1-4) | curator health + prune-entities.py + entity get loop | filed |
| `kairix entity purge --dry-run / --execute` (proper CLI, not a script) | `scripts/prune-entities.py` | filed |
| `kairix store crawl --reset` (drop-and-rebuild in one command) | manual `MATCH (n) DETACH DELETE n` + `kairix store crawl` | filed |
| Override-coverage stats (matched N times in last crawl) | shell loop over `_entity-overrides.md` + `kairix entity get` | filed |

Until those land, the steps above are the operational practice.

## See also

- [how-to-rebuild-entity-graph](how-to-rebuild-entity-graph.md) — full rebuild when audit + repair isn't enough.
- [entity-overrides user guide](../../user-guide/entity-overrides.md) — vault-driven allowlist/blocklist.
- `kairix curator health --help` — flags for the health surface this runbook leans on.
- `scripts/prune-entities.py --help` — the safe-purge primitive.
