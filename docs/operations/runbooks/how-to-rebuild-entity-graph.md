# How-to — Rebuild the entity graph from scratch

**When to use:** Neo4j entity graph is corrupted, significantly out of sync, or after a document-store restructure that invalidated large numbers of wikilinks. **Not** for normal staleness — use the incremental crawler instead.

**Time:** 2-10 minutes depending on document store size.

**Warning:** Drops all entities and relationships from the graph. Production retrieval that relies on entity boost will degrade until the rebuild completes.

---

## Configure for your environment

| Reference | Substitute with |
|---|---|
| `${KAIRIX_DOCUMENT_ROOT}` | `kairix.config.yaml` `paths.document_root`, or `KAIRIX_DOCUMENT_ROOT` env var |
| `${KAIRIX_NEO4J_URI}` | `bolt://<host>:7687`, set in your env or secrets |
| `${KAIRIX_NEO4J_USER}` | typically `neo4j` |
| `${KAIRIX_NEO4J_PASSWORD}` | from your secrets pipeline |

If kairix is in Docker, prefix host commands with `docker exec <container-name>`.

## Architecture

Entity data lives in Neo4j. Kairix populates Neo4j by walking the document store and extracting `[[wikilink]]` references — one entity node per unique wikilink target, one `MENTIONS` edge per occurrence. The crawler is `kairix store crawl`.

The kairix SQLite + usearch index is independent — search continues to work without Neo4j (entity boost is just disabled).

## Before you start

```bash
# Confirm Neo4j is reachable
kairix onboard check
# Look for: "Neo4j: ✓ connected"

# Snapshot the current entity count for comparison
kairix store health
# Note the entity count and relationship count.

# Confirm the document store is current — your sync mechanism has propagated
# any pending changes. If the doc store is stale, the rebuilt graph will
# inherit that staleness.
```

## Step 1 — Stop any running crawler

A partial crawl can leave orphan nodes. Either wait for any running crawler to complete, or fail-fast on its lockfile if your deployment uses one. Check whatever scheduler runs `kairix store crawl` periodically and pause it for the duration of the rebuild.

```bash
# Look for a running kairix store crawl
pgrep -af "kairix store crawl" || echo "no crawler running"
```

## Step 2 — Drop all entities and relationships

```bash
# Using cypher-shell (requires Neo4j password)
cypher-shell -a "${KAIRIX_NEO4J_URI}" -u "${KAIRIX_NEO4J_USER}" \
             -p "${KAIRIX_NEO4J_PASSWORD}" \
             "MATCH (n) DETACH DELETE n"

# Verify: must return 0
cypher-shell -a "${KAIRIX_NEO4J_URI}" -u "${KAIRIX_NEO4J_USER}" \
             -p "${KAIRIX_NEO4J_PASSWORD}" \
             "MATCH (n) RETURN count(n) AS remaining"
# Expected: remaining = 0
```

If you don't have `cypher-shell` available, the kairix CLI can do this:

```bash
kairix store reset --confirm
```

(Subject to operator policy — `kairix store reset` is destructive and may be disabled in your deployment.)

## Step 3 — Verify wikilinks exist in the document store

Entity relationships come from `[[wikilink]]` syntax. If the document store has no wikilinks, the rebuilt graph will be empty.

```bash
# Count files containing wikilinks
grep -rl '\[\[' "${KAIRIX_DOCUMENT_ROOT}" --include="*.md" 2>/dev/null | wc -l
# A typical knowledge store has 50+ files with wikilinks.
```

If the count is unexpectedly low, the issue is content authoring rather than kairix — fix the document store content before rebuilding.

## Step 4 — Run a full crawl

```bash
# Full document-store crawl, populating Neo4j
kairix store crawl --full

# Watch progress
tail -f "${KAIRIX_DATA_DIR}/logs/store-crawl.log"

# Healthy completion shows:
#   "scanned N documents"
#   "extracted M wikilinks"
#   "seeded P entities"
#   "created Q relationships"
#   "completed in Xs"
```

The `--full` flag forces a complete re-crawl rather than the incremental hash-comparison mode.

## Step 5 — Verify the rebuild

```bash
# Health check — entity counts must be > 0
kairix store health
# Expected: entity count > 0, relationship count > 0

# Direct Cypher confirmation
cypher-shell -a "${KAIRIX_NEO4J_URI}" -u "${KAIRIX_NEO4J_USER}" \
             -p "${KAIRIX_NEO4J_PASSWORD}" \
             "MATCH ()-[r]->() RETURN count(r) AS relationships"
# Expected: > 0

# Entity-enriched search smoke test — pick a topic with strong wikilink coverage
kairix search "<a topic from your knowledge store>" --json \
  | jq '.intent, .results[0]'
# Entity-intent results should show entity context where applicable.

# Full onboard check
kairix onboard check
# All ✓
```

## Post-rebuild — verify relationship quality

If entity-enriched search shows no entity context for a topic you expect:

1. Confirm the topic has wikilinks in the document store: `grep -rl '\[\[<Topic>\]\]' "${KAIRIX_DOCUMENT_ROOT}"`.
2. Wikilink text must match what the crawler extracts — case-sensitive, exact match. If your wikilinks use display-text aliases (`[[canonical|display]]`), the canonical form is what's indexed.
3. The crawler ignores wikilinks inside code fences and frontmatter — check the wikilinks aren't enclosed in either.

## See also

- `runbook-entity-graph-stale.md` — for incremental staleness; less invasive than a full rebuild.
- `kairix entity suggest` — discover candidate entities from new content.
- `kairix entity validate` — Wikidata cross-check on existing entities.
