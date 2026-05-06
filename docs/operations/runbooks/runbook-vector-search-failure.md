# Runbook — Vector search failure (`vec=0, vec_failed=True`)

**Severity:** P1 — search returns BM25-only results; semantic recall degraded.

**Symptom:** `kairix search "<query>" --json` shows `"vec_count": 0` and `"vec_failed": true` while `bm25_count > 0`. Only keyword results come back.

---

## What's happening

Vector search calls the configured `EmbedProvider` to embed the query, then queries the usearch vector index. Either:

1. **Embedding call fails** — credentials missing, rate-limited, or the provider endpoint is unreachable.
2. **Vector index is missing or corrupt** — usearch index file absent, or its hash dimensions mismatch the configured `KAIRIX_EMBED_DIMS`.
3. **No vectors indexed yet** — `kairix embed` hasn't run successfully against this corpus.

BM25 keeps working because it's pure SQLite FTS5 with no external dependency.

## Configure for your environment

This runbook references kairix's standard environment surface. Substitute your operator-specific paths where shown:

| Reference | Substitute with |
|---|---|
| `${KAIRIX_DATA_DIR}` | `kairix.config.yaml` `paths.db_path` parent, or `KAIRIX_DATA_DIR` env var (default `~/.cache/kairix/`) |
| `${KAIRIX_DOCUMENT_ROOT}` | `kairix.config.yaml` `paths.document_root`, or `KAIRIX_DOCUMENT_ROOT` env var |
| `<your-vm-host>` | hostname of the VM running kairix, if remote |
| `<embed-provider-creds>` | `KAIRIX_LLM_API_KEY` + `KAIRIX_LLM_ENDPOINT` (or `KAIRIX_EMBED_*` for a separate embed provider), or `OPENAI_API_KEY` for OpenAI |

If kairix is running in Docker, prefix host commands with `docker exec <container-name>`.

## Quick diagnosis

```bash
# 1. Confirm the symptom
kairix search "test query" --json | jq '{bm25_count, vec_count, vec_failed}'
# Healthy:    {"bm25_count": N, "vec_count": M, "vec_failed": false}
# Failure:    {"bm25_count": N, "vec_count": 0, "vec_failed": true}

# 2. Check vector index file exists and has rows
sqlite3 "${KAIRIX_DATA_DIR}/index.sqlite" \
  "SELECT COUNT(*) AS chunks FROM content_vectors"
# 0 rows → no embeddings yet → run kairix embed (skip to Fix C)

# 3. Check usearch index file size
ls -la "${KAIRIX_DATA_DIR}/index.usearch"
# Missing or 0 bytes → corrupt or never built (skip to Fix C)

# 4. Run the recall self-check
kairix embed recall-check
# Reports per-query hits/misses; if all skipped → credentials issue (Fix A)
```

## Fix A — Credentials missing or invalid

```bash
# Verify credentials are reachable
kairix onboard check
# Expected: "Embed provider: ✓ Azure (or OpenAI)"
# Failure:  "Embed provider: ✗ <reason>"

# Inspect the env vars kairix expects
env | grep -E "^KAIRIX_(LLM|EMBED)_" | sort
# Required: KAIRIX_LLM_API_KEY + KAIRIX_LLM_ENDPOINT
#       OR  KAIRIX_EMBED_API_KEY + KAIRIX_EMBED_ENDPOINT (separate embed provider)
#       OR  OPENAI_API_KEY (OpenAI fallback)
```

If your deployment uses a secrets file or vault to populate the environment, the operator-specific recovery procedure for that path lives in your private operations notes — typically:

- Re-run the secrets-fetch service / step that populates the env.
- Confirm the credentials file (e.g. `/run/secrets/kairix.env`) exists and is readable by the kairix process.
- Restart the kairix process so the new credentials are picked up.

## Fix B — Provider rate-limited / endpoint unreachable

```bash
# Test the embed call directly via a one-off embed
echo "smoke test" | kairix embed --limit 1 2>&1 | tail -10
# Look for HTTP 429 (rate limit), HTTP 401 (auth), or connection errors

# If 429: the provider has a quota cap. Reduce embed batch size or
#   slow the cron schedule until the cap resets.
# If 401: credentials are present but invalid — rotate or re-fetch.
# If connection error: check network/proxy from the kairix host to the
#   provider endpoint listed in KAIRIX_LLM_ENDPOINT.
```

## Fix C — No vectors indexed yet

```bash
# Run the embed pipeline against the configured document root
kairix embed

# Watch progress
kairix embed status
# Expected: "embedded N chunks, failed M, duration Xs"

# Verify chunks landed
sqlite3 "${KAIRIX_DATA_DIR}/index.sqlite" \
  "SELECT COUNT(*) FROM content_vectors"
# Should be > 0

# Smoke test
kairix search "topic that's definitely in your corpus" --json \
  | jq '{vec_count, vec_failed}'
# vec_count > 0, vec_failed: false
```

## Verify fix

```bash
# Vector search must return non-zero
kairix search "agent memory" --json | jq '{vec_count, vec_failed}'
# Expected: vec_count > 0, vec_failed: false

# Onboard check fully green
kairix onboard check
# All ✓
```

## Prevent recurrence

- Run `kairix onboard check` on every deploy and after secrets rotation.
- Wire `kairix embed` into a scheduled job (cron / systemd timer) so new content is indexed within your acceptable lag window.
- Monitor `KAIRIX_DATA_DIR/logs/embed.log` for repeating failures and alert on `failed > 0` for two consecutive runs.

## See also

- `runbook-embedding-lag.md` — new content not appearing in search after the expected embed cycle.
- `runbook-benchmark-regression.md` — search quality degraded across the board (NDCG drop).
- Operator-specific overlays: your private runbooks for credential rotation, secrets-fetch service, and binary-symlink layout (these are deployment-specific and stay private).
