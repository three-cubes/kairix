# Runbook — Embedding lag (new content not searchable)

**Severity:** P2 — search results stale relative to the document store.

**Symptom:** Documents added or modified more than your embed cycle ago (typically 15-60 minutes) don't appear in `kairix search` results. New chunks should land in `content_vectors` within one cycle of `kairix embed`.

---

## What's happening

`kairix embed` is the pipeline that:

1. Scans `${KAIRIX_DOCUMENT_ROOT}` for new/changed markdown files (`DocumentScanner`).
2. Hashes content; skips unchanged docs.
3. Calls the configured `EmbedProvider` to generate vectors for new chunks.
4. Writes chunks into `content_vectors` and the usearch ANN index.

If new content isn't searchable, one of those four stages is stuck or failing.

## Configure for your environment

| Reference | Substitute with |
|---|---|
| `${KAIRIX_DATA_DIR}` | `kairix.config.yaml` `paths.db_path` parent, or `KAIRIX_DATA_DIR` env var |
| `${KAIRIX_DOCUMENT_ROOT}` | `kairix.config.yaml` `paths.document_root`, or `KAIRIX_DOCUMENT_ROOT` env var |
| `<embed-cycle>` | how often your scheduled `kairix embed` runs (e.g. 15min via cron, hourly via systemd timer) |
| `<sync-mechanism>` | your document store sync — Obsidian Sync, git pull, rsync, etc. — kairix doesn't ship one |

## Quick diagnosis

```bash
# 1. Are new files even on disk?
find "${KAIRIX_DOCUMENT_ROOT}" -name "*.md" -newer "${KAIRIX_DATA_DIR}/index.sqlite" \
  | head -10
# Empty → your sync mechanism hasn't propagated the new docs yet (Cause A)

# 2. Did the last embed run succeed?
tail -30 "${KAIRIX_DATA_DIR}/logs/embed.log"
# Look for "embedded N chunks, failed 0" with a recent timestamp.

# 3. Is the embed pipeline actually scheduled?
# Check whatever scheduler your deployment uses — cron, systemd timer, k8s CronJob.

# 4. Run an embed manually and watch
kairix embed
# Expected: "Scanned documents: N new, M updated, K unchanged"
#           "Embedded N chunks, failed 0"
```

## Cause A — Document store sync hasn't propagated

The new content you're searching for isn't on disk where kairix is looking.

```bash
# Confirm the file exists at the expected path
ls -la "${KAIRIX_DOCUMENT_ROOT}/<your-new-doc>.md"

# Verify document_root is what you expect
kairix onboard check
# Look for: "Document root: <path> (✓ exists)"
```

This is typically a sync issue outside kairix. Restart your sync mechanism or pull the source manually.

## Cause B — Embed pipeline failing

```bash
# Run the embed pipeline interactively to see live errors
kairix embed

# Common error signatures and fixes:

# "Embed credentials not set" → see runbook-vector-search-failure.md (Fix A)

# "401 Unauthorized" / "Authorization failed"
#   → credentials present but invalid or expired; rotate via your secrets path

# "429 Too Many Requests"
#   → embed provider rate limit. Either:
#     - reduce concurrent embed runs (single scheduled instance)
#     - lower batch size (KAIRIX_EMBED_BATCH_SIZE env var; default 250)
#     - upgrade provider quota

# "disk I/O error" / database is locked
#   → another kairix process is writing to the same DB. Check the lockfile
#     at ${KAIRIX_DATA_DIR}/embed.lock and PID inside it. Kill the stale
#     process and retry.

# "No relevant docs found" but you know files exist
#   → check the collection definitions in kairix.config.yaml resolve to
#     your new content's location. `kairix config validate` will flag
#     unreachable paths.
```

## Cause C — Scheduled embed not running

If `kairix embed` works manually but stops between scheduled runs:

- Confirm the scheduler entry exists for the user kairix runs as.
- Confirm the scheduler's environment includes `KAIRIX_*` and any provider credentials (cron resets `PATH` and most env vars by default).
- Confirm the lockfile at `${KAIRIX_DATA_DIR}/embed.lock` isn't stale — kairix does refuse to start a new run if a lock exists for a live PID.

```bash
# Check for stale lockfile
test -f "${KAIRIX_DATA_DIR}/embed.lock" && cat "${KAIRIX_DATA_DIR}/embed.lock"
# Compare PID against running processes; if dead, kairix self-clears it on next run.

# If schedule still doesn't fire, the scheduler itself is the issue
# — that's deployment-specific, see your private operator notes.
```

## Verify fix

```bash
# Trigger embed manually
kairix embed

# Confirm completion
tail -10 "${KAIRIX_DATA_DIR}/logs/embed.log"
# "Embedded N chunks, failed 0" — look for N > 0 if there were new docs

# Smoke test with content you just added
kairix search "title or distinctive phrase from the new document"
# Expected: the new document appears in results
```

## Prevent recurrence

- Schedule `kairix embed` at a cadence that matches your acceptable lag (15min for active vaults; hourly for stable ones).
- Wire the embed log to your monitoring — alert on `failed > 0` for two consecutive runs.
- Keep `kairix onboard check` in your post-deploy smoke test so credential rot is caught early.

## See also

- `runbook-vector-search-failure.md` — vector search returns zero (different symptom; embedding may be working but query-time lookup fails).
- `runbook-benchmark-regression.md` — search quality dropped (different symptom; new content is searchable but ranking shifted).
