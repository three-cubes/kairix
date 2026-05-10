# Operations Guide

Step-by-step deployment and operations guide for Kairix on a server. This document is the single source of truth for getting a new deployment running and keeping it healthy.

For benchmark methodology and current scores see [EVALUATION.md](EVALUATION.md).

---

## Configuration vs Secrets

Not all environment variables are secrets. Configuration values belong in `service.env` or `docker-compose.override.yml`. Secrets belong in Key Vault or `/run/secrets/` (tmpfs).

**Configuration (service.env / compose environment):**

| Variable | Purpose | Default |
|----------|---------|---------|
| `KAIRIX_EMBED_DIMS` | Embedding vector dimensions | `1536` |
| `KAIRIX_AZURE_API_VERSION` | Azure API version override | `2024-12-01-preview` |
| `KAIRIX_NEO4J_URI` | Neo4j connection URI | `bolt://localhost:7687` |
| `KAIRIX_NEO4J_USER` | Neo4j username | `neo4j` |
| `KAIRIX_DOCUMENT_ROOT` | Path to document store | `~/kairix-vault` |
| `KAIRIX_DB_PATH` | SQLite database path | `~/.cache/kairix/index.sqlite` |
| `KAIRIX_KV_NAME` | Azure Key Vault name (for secret resolution) | — |

**Secrets (Key Vault / /run/secrets/ / env var override):**

| KV name | Env var | Purpose |
|---------|---------|---------|
| `kairix-llm-api-key` | `KAIRIX_LLM_API_KEY` | LLM API key |
| `kairix-llm-endpoint` | `KAIRIX_LLM_ENDPOINT` | LLM API endpoint |
| `kairix-llm-model` | `KAIRIX_LLM_MODEL` | Chat model name |
| `kairix-embed-api-key` | `KAIRIX_EMBED_API_KEY` | Embed API key (falls back to LLM) |
| `kairix-embed-endpoint` | `KAIRIX_EMBED_ENDPOINT` | Embed API endpoint (falls back to LLM) |
| `kairix-embed-model` | `KAIRIX_EMBED_MODEL` | Embed model name |
| `kairix-neo4j-password` | `KAIRIX_NEO4J_PASSWORD` | Neo4j password |

Resolution order: env var > per-file secret (`/run/secrets/<name>`) > bundle file (`/run/secrets/kairix.env`) > Azure Key Vault CLI (`KAIRIX_KV_NAME`).

---

## Environment Configuration

All infrastructure-specific values (vault name, paths, credentials) are passed via environment variables — nothing is hardcoded in the source. The repo ships [`env.example`](../env.example) with every variable documented.

**Setting up your environment file:**

```bash
# On your deployment VM
cp env.example /opt/kairix/service.env
chmod 600 /opt/kairix/service.env
# Edit with your values (Key Vault name, vault path, data dir, etc.)
nano /opt/kairix/service.env

# Source it in each cron job (see Cron Scheduling below)
source /opt/kairix/service.env
```

**For local dev/testing:**

```bash
cp env.example .env    # .env is gitignored
# Edit with your values, then:
source .env && kairix search "test query" --agent builder
```

**For GitHub Actions:** add each variable as a repository secret (Settings → Secrets and variables → Actions). The CI workflows that need Azure credentials read them as `${{ secrets.KAIRIX_LLM_API_KEY }}` etc.

**Key variables to set first:**

| Variable | What it is |
|---|---|
| `KAIRIX_KV_NAME` | Your Azure Key Vault name |
| `KAIRIX_VAULT_ROOT` | Path to your Obsidian vault |
| `KAIRIX_DATA_DIR` | Where logs and data files go |
| `KAIRIX_WORKSPACE_ROOT` | Agent memory log root (e.g. `/data/workspaces`) |
| `LOG_DIR` | Where deploy.sh and cron wrappers write logs |

See `env.example` for the complete variable reference.

---

## Prerequisites

### 1. Azure Resources

You need an Azure subscription with the following resources:

**Azure OpenAI resource** (Australia East recommended for data residency)
- Deployment: `text-embedding-3-large` (1536-dim, for embedding)
- Deployment: `gpt-4o-mini` (for briefing, classification, entity extraction)

**Azure Key Vault** — set `KAIRIX_KV_NAME` env var to your vault name (e.g. `my-project-kv`)
- Used to store API credentials at runtime — credentials are never hardcoded or stored in env files

Create the following secrets in Key Vault:

| Secret name | Value |
|---|---|
| `kairix-llm-endpoint` | `https://<your-resource>.cognitiveservices.azure.com/` |
| `kairix-llm-api-key` | Your Azure OpenAI API key |
| `kairix-embed-model` | `text-embedding-3-large` (or your deployment name) |
| `kairix-llm-model` | `gpt-4o-mini` (or your deployment name) |
| `kairix-neo4j-password` | Your Neo4j password |

```bash
# Create secrets (run once, from a machine with Key Vault access)
az keyvault secret set --vault-name ${KAIRIX_KV_NAME} --name kairix-llm-endpoint \
  --value "https://your-resource.cognitiveservices.azure.com/"
az keyvault secret set --vault-name ${KAIRIX_KV_NAME} --name kairix-llm-api-key \
  --value "your-api-key"
az keyvault secret set --vault-name ${KAIRIX_KV_NAME} --name kairix-embed-model \
  --value "text-embedding-3-large"
az keyvault secret set --vault-name ${KAIRIX_KV_NAME} --name kairix-llm-model \
  --value "gpt-4o-mini"
```

### 2. Azure Authentication on the VM

The VM running Kairix must be able to authenticate to Azure Key Vault. Two options:

**Option A: Azure Managed Identity (recommended for production)**
- Assign a system-assigned or user-assigned managed identity to the VM
- Grant the identity `Key Vault Secrets User` role on the Key Vault
- No credentials needed on the VM — `az keyvault secret show` works automatically

```bash
# Verify managed identity auth is working
az keyvault secret show --vault-name ${KAIRIX_KV_NAME} --name kairix-llm-endpoint --query value -o tsv
```

**Option B: Service Principal**
- Create a service principal with Key Vault Secrets User access
- Set `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_TENANT_ID` in the service env file
- Or use `az login --service-principal` in the deploy script

### 3. Kairix Index

Kairix owns its own SQLite database for full-text search (FTS5) and vector storage (usearch HNSW). No external search tool is required.

```bash
# Run the initial index build
kairix embed

# Verify the index exists
ls ~/.cache/kairix/index.sqlite

# Check index health
kairix onboard check
```

**usearch:** Installed automatically as a pip dependency (`usearch>=2.0`). No manual extension path configuration needed.

### 4. Neo4j (optional — entity graph)

Neo4j Community Edition powers entity boost, alias resolution, and multi-hop query planning. All other kairix features work without it.

Neo4j Community Edition is licensed under **GPL v3**. Kairix communicates via the Bolt protocol using the Apache 2.0 Python driver — no GPL3 code is bundled with kairix.

**Install:**

```bash
# Install script (Docker default; --apt option also available)
bash <(curl -fsSL https://raw.githubusercontent.com/quanyeomans/kairix/main/scripts/install-neo4j.sh)

# Or quick Docker start (no install script):
docker run -d --name neo4j -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/$(openssl rand -hex 16) \
  neo4j:5-community
```

After installing, set in `service.env` or `/opt/kairix/service.env`:
```
KAIRIX_NEO4J_URI=bolt://localhost:7687
KAIRIX_NEO4J_USER=neo4j
KAIRIX_NEO4J_PASSWORD=<your-password>
```

For managed deployments where the password is stored in Azure Key Vault as `kairix-neo4j-password`, `kairix-fetch-secrets.service` populates `KAIRIX_NEO4J_PASSWORD` in `/run/secrets/kairix.env` automatically.

Verify Neo4j is reachable:
```bash
kairix onboard check
# → neo4j_reachable: ✓  Neo4j reachable — N nodes in graph
```

### 5. Infrastructure Directories

Create the required directories before first run:

```bash
# Set KAIRIX_DATA_DIR and KAIRIX_WORKSPACE_ROOT to your preferred locations
sudo mkdir -p ${KAIRIX_DATA_DIR:-/var/lib/kairix}/briefing
sudo mkdir -p ${KAIRIX_DATA_DIR:-/var/lib/kairix}/logs
sudo mkdir -p ${KAIRIX_WORKSPACE_ROOT:-/var/lib/kairix/workspaces}
sudo chown -R <service-user>:<service-user> \
  ${KAIRIX_DATA_DIR:-/var/lib/kairix} \
  ${KAIRIX_WORKSPACE_ROOT:-/var/lib/kairix/workspaces}
```

Kairix expects:
- `$KAIRIX_VAULT_ROOT` — vault root (kairix indexes this)
- `$KAIRIX_DATA_DIR/briefing/` — session briefings output directory
- `$KAIRIX_DATA_DIR/logs/` — optional query logs (`KAIRIX_LOG_QUERIES=1`)
- `$KAIRIX_WORKSPACE_ROOT/<agent>/memory/` — agent memory logs (required for briefing pipeline)

---

## Installation

### Docker Compose (recommended)

Docker Compose is the primary deployment method. It bundles kairix, Neo4j, and the secrets sidecar in a single stack.

```bash
# Clone and start
git clone https://github.com/quanyeomans/kairix.git
cd kairix/docker
cp .env.example .env
# Edit .env with your values (Key Vault name, vault path, etc.)
docker compose up -d
```

See `docker/docker-compose.yml` for the full service definition. `kairix onboard check` runs inside the container on startup.

### systemd unit (recommended for reboot-survivable VM deployments)

If you run kairix as a systemd-managed Docker stack on a long-running VM, copy the example units from `scripts/install/` and tailor them. They pin the correct dependency ordering (kairix.service → kairix-fetch-secrets.service → docker.service) so the deployment self-heals after a reboot rather than crash-looping when `/run/secrets/kairix.env` is empty (resolved in v2026.5.10, see #167).

```bash
sudo install -m 0644 scripts/install/kairix.service.example /etc/systemd/system/kairix.service
sudo install -m 0644 scripts/install/kairix-fetch-secrets.service.example /etc/systemd/system/kairix-fetch-secrets.service
sudo install -m 0755 scripts/install/permissions-preflight.sh /opt/kairix/bin/permissions-preflight.sh
sudo systemctl daemon-reload
sudo systemctl enable --now kairix-fetch-secrets.service kairix.service
```

`permissions-preflight.sh` runs as `ExecStartPre=` and:

- Fixes `.env` ownership/mode if root + service-user mismatch (the #167 root cause).
- Fails fast if `/run/secrets/kairix.env` is missing or empty.
- Verifies that `KAIRIX_LLM_API_KEY`, `KAIRIX_LLM_ENDPOINT`, `KAIRIX_EMBED_API_KEY`, `KAIRIX_EMBED_ENDPOINT` are all populated when the service-env and secrets file are merged.

A failed preflight surfaces as an actionable journalctl line — far more useful than docker compose's "permission denied" loop.

### Health probes

Kairix exposes two health endpoints from the MCP HTTP transport:

| Endpoint | Purpose | Body shape |
|---|---|---|
| `GET /healthz` | Basic liveness — process up, started_at clock past zero. Back-compat. | `{"ready": bool, "uptime_s": int}` |
| `GET /healthz/ready` | Layered readiness — granular capability checks. Use this from your load balancer. | `{"live": bool, "ready": bool, "uptime_s": int, "checks": {"secrets_loaded": bool, "vector_search_capable": bool, "bm25_search_capable": bool, "detail": {...}}}` |

`/healthz/ready` is the actionable signal: `ready=true` means the deployment is fully operational (secrets loaded AND vector search capable). A degraded deployment that has lost vector search will report `ready=false` with `vector_search_capable=false` and a `detail` message — far better than the pre-v2026.5.10 behaviour where `/healthz` returned `ready=true` while semantic search was silently broken (#167).

### Alternative: pip install

For environments where Docker is unavailable, kairix can be installed as a pip package.

```bash
# One-line deploy (downloads and runs install.sh from the public repo)
bash <(curl -fsSL https://raw.githubusercontent.com/quanyeomans/kairix/main/scripts/install.sh)
```

This creates `/opt/kairix/.venv/` (legacy pip path), installs kairix into it, installs the wrapper script, and creates the `/usr/local/bin/kairix` symlink. After this, `kairix --help` works from any shell.

**Manual pip install:**

```bash
# Create venv and install (core)
python3 -m venv /opt/kairix/.venv
/opt/kairix/.venv/bin/pip install kairix-agentic-knowledge-mgt

# With Neo4j entity graph support (recommended for full feature set)
/opt/kairix/.venv/bin/pip install "kairix-agentic-knowledge-mgt[neo4j]"

# With MCP server for agent integration
/opt/kairix/.venv/bin/pip install "kairix-agentic-knowledge-mgt[agents]"

# Verify
/opt/kairix/.venv/bin/kairix --help
```

### Operator configuration

Kairix itself is the retrieval engine. Operator-specific configuration (vault paths, Azure credentials, agent names, private benchmark suites) is kept separately — **not inside the kairix source tree**.

The expected layout on the VM:
```
/opt/kairix/
  .venv/              ← kairix package installed here (legacy pip path)
  bin/
    kairix-wrapper.sh ← env loader; /usr/local/bin/kairix symlinks here
  service.env         ← operator config (KAIRIX_KV_NAME, KAIRIX_VAULT_ROOT, etc.)
  secrets/            ← optional: pre-fetched secrets for non-Docker deployments
```

For production deployments: operator config (service.env, private benchmark suites) should live in a separate private configuration repo, not in the kairix package.

### Upgrading

```bash
/opt/kairix/.venv/bin/pip install --upgrade git+https://github.com/quanyeomans/kairix
kairix onboard check   # verify after upgrade
```

---

## Wrapper Script and PATH Setup

**This is required for agents.** If you skip this, agents calling `kairix` will get either "command not found" or vector search failures (BM25-only fallback) because the raw Python binary has no environment loaded.

The kairix wrapper (`scripts/kairix-wrapper.sh`) loads `service.env` and `/run/secrets/kairix.env` before exec'ing the real binary. The system symlink must point to the wrapper, not the Python binary.

### Automated (recommended)

```bash
bash scripts/install.sh
```

This installs the wrapper, creates/updates the symlink, and sets up `/etc/profile.d/kairix.sh` so every shell and agent exec context has kairix on PATH.

### Manual

```bash
# Install wrapper
sudo mkdir -p /opt/kairix/bin
sudo cp scripts/kairix-wrapper.sh /opt/kairix/bin/kairix-wrapper.sh
sudo chmod 755 /opt/kairix/bin/kairix-wrapper.sh

# Create or update the symlink (replace existing if it points to raw Python binary)
sudo ln -sf /opt/kairix/bin/kairix-wrapper.sh /usr/local/bin/kairix

# Add to PATH for all sessions
sudo bash -c 'echo "export PATH=/usr/local/bin:\$PATH" > /etc/profile.d/kairix.sh'
sudo chmod 644 /etc/profile.d/kairix.sh

# Verify the symlink points to the wrapper (not the Python binary)
ls -la /usr/local/bin/kairix
readlink /usr/local/bin/kairix
# Should show: /opt/kairix/bin/kairix-wrapper.sh
```

### Verify wrapper is working

```bash
kairix onboard check
```

All checks should pass. Specifically look for `wrapper_installed: ✓` and `secrets_loaded: ✓`. If `vec_failed: true` appears in the vector search check, the wrapper isn't loading secrets — check that `service.env` has `KAIRIX_KV_NAME` set.

---

## First-Run Sequence

Run these in order on a fresh deployment. Each step must succeed before the next.

### Step 1: Deploy wrapper and PATH

```bash
bash scripts/install.sh
```

Or follow the manual steps in [Wrapper Script and PATH Setup](#wrapper-script-and-path-setup).

### Step 2: Populate service.env

```bash
# If service.env doesn't exist yet
cp env.example /opt/kairix/service.env
nano /opt/kairix/service.env
# Set: KAIRIX_KV_NAME, KAIRIX_VAULT_ROOT, KAIRIX_DATA_DIR, KAIRIX_WORKSPACE_ROOT
```

### Step 3: Verify Azure credentials

```bash
source /opt/kairix/service.env

ENDPOINT=$(az keyvault secret show --vault-name ${KAIRIX_KV_NAME} \
  --name kairix-llm-endpoint --query value -o tsv)
APIKEY=$(az keyvault secret show --vault-name ${KAIRIX_KV_NAME} \
  --name kairix-llm-api-key --query value -o tsv)

echo "Endpoint: ${ENDPOINT:0:40}..."
echo "Key: ${APIKEY:0:8}..."
```

Both must return values. If either is empty, check Azure CLI auth and Key Vault access policy.

### Step 4: Run a test embed (first 20 chunks)

```bash
KAIRIX_LLM_ENDPOINT="$ENDPOINT" \
KAIRIX_LLM_API_KEY="$APIKEY" \
kairix embed --limit 20
```

Expected output:
```
INFO  Starting embed — pending=20
INFO  Embedded batch 0 (20 chunks)
INFO  Running post-embed recall check...
INFO  Recall: 4/5 (80%)
INFO  Done — embedded=20 failed=0 duration=12s cost=$0.0005
```

If you see `SchemaVersionError` or `usearch index load failed`, see [Troubleshooting](#troubleshooting).

### Step 5: Full vault embed

```bash
KAIRIX_LLM_ENDPOINT="$ENDPOINT" \
KAIRIX_LLM_API_KEY="$APIKEY" \
nohup kairix embed >> ${KAIRIX_DATA_DIR:-/var/lib/kairix}/logs/embed.log 2>&1 &
echo "PID: $!"
```

For a typical vault this takes 10–30 minutes and costs ~$0.30–0.50 at 1536-dim, depending on size. Monitor with:
```bash
tail -f ${KAIRIX_DATA_DIR:-/var/lib/kairix}/logs/embed.log
```

Done when you see: `Done — embedded=N failed=0`

### Step 6: Verify search works

```bash
kairix search "what are our engineering standards" --agent builder --json
```

Expected: `vec_count > 0` and 3–5 results with file paths. If `vec_failed: true`, the wrapper isn't loading credentials — run `kairix onboard check`.

### Step 7: Populate the entity graph

```bash
kairix vault crawl --vault-root /path/to/vault
kairix curator health   # should report entity counts
```

Expected: entity count ≥ 50 for a typical vault.

### Step 8: Test briefing

```bash
KAIRIX_LLM_ENDPOINT="$ENDPOINT" \
KAIRIX_LLM_API_KEY="$APIKEY" \
kairix brief builder
```

Output written to `$KAIRIX_DATA_DIR/briefing/builder-latest.md`. Verify it's non-empty and coherent.

### Step 9: Install agent usage guide

```bash
kairix onboard guide --vault-root /path/to/vault
kairix embed --changed   # make the guide searchable
```

This installs `docs/user-guide/agent-usage-guide.md` into the document store's shared knowledge base so agents can search for kairix usage instructions.

### Step 10: Register cron jobs

See [Cron Scheduling](#cron-scheduling) below.

---

## Cron Scheduling

Two recurring jobs are required for a production deployment.

### Secrets in cron jobs

Cron jobs must source credentials from the tmpfs secrets file populated by `kairix-fetch-secrets.service` — do not fetch secrets inline in cron entries.

```bash
# Correct pattern — source the secrets file written by kairix-fetch-secrets.service
source "${KAIRIX_SECRETS_FILE:-/run/secrets/kairix.env}"
kairix embed

# Wrong — fetches secrets inline, requires az CLI auth per-run, leaks into cron logs
export KAIRIX_LLM_API_KEY=$(az keyvault secret show ...)
```

For production VM deployments, `kairix-fetch-secrets.service` writes Azure credentials to `/run/secrets/kairix.env` (tmpfs) at boot using the VM's managed identity. See [SECURITY.md](../SECURITY.md) for setup detail.

### Incremental embed (new vault files)

Runs kairix embed incrementally — only embeds files modified since the last run. Exits quickly (embedded=0) when nothing has changed. Schedule to run frequently (e.g. hourly).

Your cron wrapper should source credentials before running:
```bash
# Example wrapper pattern
source "${KAIRIX_SECRETS_FILE:-/run/secrets/kairix.env}"
kairix embed >> ${KAIRIX_DATA_DIR:-/var/lib/kairix}/logs/embed.log 2>&1
```

See [`scripts/cron/kairix-embed.sh`](scripts/cron/kairix-embed.sh) for the reference implementation.

### Nightly entity + relationship seed

Runs vault crawler and relationship seeding. Uses GPT-4o-mini for relationship classification. Schedule nightly during low-usage hours.

```bash
# The two commands to run, in order:
kairix vault crawl --vault-root $KAIRIX_VAULT_ROOT
python scripts/seed-entity-relations.py
```

See [`scripts/cron/`](scripts/cron/) for reference cron wrapper scripts.

### Verifying cron jobs are registered

```bash
crontab -l
```

### Verifying cron jobs ran successfully

```bash
# Check embed log
grep "Done —" ${KAIRIX_DATA_DIR:-/var/lib/kairix}/logs/embed.log | tail -5

# Check entity log
tail -20 ${KAIRIX_DATA_DIR:-/var/lib/kairix}/logs/entity-relation-seed.log
```

---

## Environment Variables

All credentials are fetched from Azure Key Vault at runtime. You can override any value with environment variables for testing:

| Variable | Purpose | Default |
|---|---|---|
| `KAIRIX_LLM_API_KEY` | Azure OpenAI API key | From Key Vault `kairix-llm-api-key` |
| `KAIRIX_LLM_ENDPOINT` | Azure OpenAI endpoint URL | From Key Vault `kairix-llm-endpoint` |
| `KAIRIX_EMBED_MODEL` | Embedding deployment name | From Key Vault `kairix-embed-model` |
| `KAIRIX_VAULT_ROOT` | Path to Obsidian vault | `/path/to/vault` |
| `KAIRIX_DATA_DIR` | Data directory for logs | `/var/lib/kairix` |
| `KAIRIX_WORKSPACE_ROOT` | Agent memory log root | `/data/workspaces` |
| `KAIRIX_NEO4J_URI` | Neo4j Bolt URI | `bolt://localhost:7687` |
| `KAIRIX_NEO4J_USER` | Neo4j username | `neo4j` |
| `KAIRIX_LOG_QUERIES` | Set to `1` to log all search queries | Off |
| `KAIRIX_USEARCH_PATH` | Override usearch index file path | `~/.cache/kairix/vectors.usearch` |

---

## Summarise Pipeline

After embedding, kairix automatically generates L0 (abstract-level) summaries for each document and stores them in `summaries.db`. These summaries improve search quality by giving the ranking engine a concise representation of each document's content.

- **Runs automatically** after `kairix embed` completes.
- Summaries are stored in a separate SQLite database (`summaries.db` in the data directory).
- To skip summarisation (e.g. for a quick test embed), pass `--skip-summarise`:
  ```bash
  kairix embed --skip-summarise
  ```
- To run summarisation independently:
  ```bash
  kairix summarise
  ```

---

## Optional Extras

### Cross-encoder re-ranking (`[rerank]`)

For MULTI_HOP and SEMANTIC intent queries, kairix can apply a cross-encoder re-ranker after initial retrieval to improve result ordering. This requires the `rerank` extra:

```bash
pip install "kairix-agentic-knowledge-mgt[rerank]"
```

Re-ranking is applied automatically when the extra is installed. Without it, kairix falls back to the standard fusion ranking (no degradation, just no cross-encoder pass).

### Entity suggestion (`[nlp]`)

Entity suggestion uses spaCy NLP models to detect named entities in your documents. This requires the `nlp` extra:

```bash
pip install "kairix-agentic-knowledge-mgt[nlp]"
```

This is required for `kairix entity suggest` to work, including inside Docker containers. The Docker image includes the `nlp` extra by default.

---

## Running the Benchmark

For local / non-production hosts:

```bash
kairix benchmark run --suite suites/example.yaml
```

For shared production VMs, run inside the **sandboxed eval container** (closes #88) so eval workloads can't starve agent traffic:

```bash
docker compose -f docker-compose.yml \
               -f docker-compose.override.yml \
               -f docker-compose.eval.yml \
               --profile eval \
    run --rm kairix-eval benchmark run --suite suites/reflib-gold.yaml
```

The Dockerfile's entrypoint auto-prepends `kairix`, so the leading `kairix` is omitted from the run command. The eval profile pins `cpus=1.0` and `mem_limit=2g` (vs production's 4 CPU / 3 GB), so a benchmark run can't pin the host. The container exits when the command finishes; `--rm` cleans up. See [docker-compose.eval.yml](../../docker-compose.eval.yml) for the full overlay.

See [EVALUATION.md](../evaluation/EVALUATION.md) for current scores, benchmark methodology, and the graded relevance scoring format.

---

## Monitoring

### What to check daily

```bash
# Embed ran and found/embedded the right number of files
grep "Done —" ${KAIRIX_DATA_DIR:-/var/lib/kairix}/logs/embed.log | tail -3

# No dimension mismatch errors (would indicate concurrent index writers)
grep -i "dimension mismatch" ${KAIRIX_DATA_DIR:-/var/lib/kairix}/logs/embed.log | tail -5

# Entity crawler ran cleanly
tail -5 ${KAIRIX_DATA_DIR:-/var/lib/kairix}/logs/entity-relation-seed.log

# Vector count is stable or growing
kairix onboard check

# Entity graph health
kairix curator health
```

### Key metrics to track

- **Vector count:** Should grow as vault grows. Sudden drop indicates index rebuild issue.
- **Entity count:** Grows as new entity stubs are added and vault crawler runs. Check with `kairix curator health`.
- **Entity graph density:** Growing node/relationship counts improve entity-aware retrieval.
- **Recall gate:** Post-embed recall check in embed log — should be ≥ 4/5. If < 4/5, run `kairix embed --force`.

### Enabling query logging

```bash
export KAIRIX_LOG_QUERIES=1
# Queries logged to ${KAIRIX_DATA_DIR:-/var/lib/kairix}/logs/queries.jsonl
# Analyse with:
.venv/bin/python scripts/analyze_queries.py
```

---

## Troubleshooting

### `kairix: command not found`

kairix is not on PATH for the current session.

```bash
# Quick fix for current session
export PATH=/usr/local/bin:$PATH
kairix --help

# Permanent fix: run the deploy script
bash scripts/install.sh

# Or manually check where the symlink is
ls -la /usr/local/bin/kairix
```

For agent exec contexts specifically (agents running commands via shell), ensure `/etc/profile.d/kairix.sh` exists and contains the PATH export. Non-login shells don't source `/etc/profile.d/` automatically — the cron wrapper or agent exec script must `source /etc/profile.d/kairix.sh` or set PATH explicitly.

### `vec_failed: true` — Vector search broken, BM25 only

Azure credentials aren't loaded for the kairix process.

```bash
# Diagnose
kairix onboard check

# Most common cause: symlink points to raw Python binary
ls -la /usr/local/bin/kairix
readlink /usr/local/bin/kairix
# If this shows .venv/bin/kairix, the wrapper isn't installed:
bash scripts/install.sh

# Alternative: verify manually
which kairix
head -1 $(which kairix)
# Should show: #!/usr/bin/env bash  (not #!/path/to/python)
```

### `KAIRIX_LLM_API_KEY not set`

The embed or briefing command can't find Azure credentials.

```bash
# Check Key Vault auth
az account show
az keyvault secret show --vault-name ${KAIRIX_KV_NAME} --name kairix-llm-api-key --query value -o tsv
```

If `az account show` fails, run `az login` or check the VM's managed identity assignment.

### `usearch index load failed`

The usearch library or index file can't be found.

```bash
# Check if usearch is available
python3 -c "import usearch; print(usearch.__version__)"

# Check if the index file exists
ls -la ~/.cache/kairix/vectors.usearch

# Override index path manually
export KAIRIX_USEARCH_PATH="/path/to/vectors.usearch"
kairix embed --limit 5
```

### `SchemaVersionError: missing columns`

The database schema has changed between versions.

```bash
# Check kairix version
kairix --version

# Run schema compatibility tests
.venv/bin/pytest tests/ -k "schema" -v

# If tests pass, bump the version in schema.py and pyproject.toml
```

### Vector search returns 0 results

The embed pipeline hasn't run, or the usearch vector index is empty or missing.

```bash
# Check if the usearch index file exists
ls -la ~/.cache/kairix/vectors.usearch
# Should exist and be > 0 bytes

# Check dimensions from metadata
cat ~/.cache/kairix/vectors.meta.json
# Look for "ndim": 1536

# If index is missing: run full re-embed
KAIRIX_LLM_ENDPOINT="$ENDPOINT" KAIRIX_LLM_API_KEY="$APIKEY" \
kairix embed --force
```

### `Dimension mismatch` errors in embed log

A dimension mismatch is now auto-detected: the old index is deleted and rebuilt with the correct dimensions on the next embed run. No manual intervention required.

### Embedding model mismatch

If another tool writes embeddings with a different model or dimension to the same database, it causes dimension mismatch errors or `vec=0` results.

**Detect:**
```bash
# Check for mixed embedding models in content_vectors
sqlite3 ~/.cache/kairix/index.sqlite \
  "SELECT model, COUNT(*) FROM content_vectors GROUP BY model;"
# If you see two models, the conflict is active
```

**Fix:**
1. Ensure no other tool writes embeddings to the kairix database — only `kairix embed` should write vectors
2. Force-rebuild Azure vectors: `kairix embed --force`
3. Verify: the query above should show only `text-embedding-3-large`

### Neo4j unavailable

kairix degrades gracefully — entity boost and multi-hop queries are disabled, but search still works.

```bash
# Check Neo4j is running
systemctl status neo4j

# Check connection settings
echo $KAIRIX_NEO4J_URI   # should be bolt://localhost:7687

# Populate entity graph after fixing
kairix vault crawl --vault-root $KAIRIX_VAULT_ROOT
```

### Nightly entity extraction not running

```bash
# Check cron is registered
crontab -l

# Check log for last run
tail -20 ${KAIRIX_DATA_DIR:-/var/lib/kairix}/logs/entity-relation-seed.log

# Run manually to debug
kairix vault crawl --vault-root $KAIRIX_VAULT_ROOT
python scripts/seed-entity-relations.py
```

### Briefing output is empty or incoherent

```bash
# Check memory logs exist for the agent
ls /data/workspaces/<agent>/memory/ | tail -5

# Check entity graph has content
kairix curator health

# Run briefing with debug output
KAIRIX_LOG_QUERIES=1 kairix brief <agent> --budget 5000
```

### More detailed runbooks

For deeper diagnostic procedures and less common failure modes, see [`docs/operations/runbooks/INDEX.md`](runbooks/INDEX.md).

---

## Upgrading

### Upgrading Kairix

```bash
# Upgrade to latest
/opt/kairix/.venv/bin/pip install --upgrade kairix-agentic-knowledge-mgt

# Or pin to a specific version
/opt/kairix/.venv/bin/pip install "kairix-agentic-knowledge-mgt==2026.4.27"

# Verify
kairix onboard check
```

If the wrapper script has changed in the new version, re-run:
```bash
bash scripts/install.sh --skip-smoke   # re-downloads and re-installs wrapper
```

### Upgrading kairix

```bash
# Install new version
pip install --force-reinstall --no-deps "kairix-agentic-knowledge-mgt==<new-version>"

# Run schema tests to verify compatibility
pytest tests/ -k "schema" -v

# Run onboard check
kairix onboard check
```

---

## Data Residency

Vault content is sent to Azure OpenAI (Australia East) for:
- **Embedding:** All vault documents sent to `text-embedding-3-large` for indexing
- **Briefing synthesis:** Memory logs + retrieved chunks sent to `gpt-4o-mini`
- **Entity extraction:** Entity stub content sent to `gpt-4o-mini` for NER
- **Relationship classification:** Relationship text sent to `gpt-4o-mini`

No vault content is stored externally beyond the duration of the API request. All vectors, entity data, and briefings live in SQLite and Neo4j on your own infrastructure.

See [SECURITY.md](../SECURITY.md) for the full data handling and secret management policy.
