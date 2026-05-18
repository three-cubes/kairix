---
type: adr
id: ADR-017
title: Deployment architecture
status: active
date: 2026-05-18
related:
  - retrieval-boost-configuration
---

# ADR-017: Deployment Architecture

**Status:** Accepted
**Date:** 2026-04-26

---

## Decision

**Docker Compose is the primary install path.** It provides the full experience — search, entity graph, background indexing — with no components for the user to install or configure separately. Neo4j is included in the stack and just works.

**pip install is the fallback** for environments where Docker is not available (e.g. locked-down corporate machines). Entity search is not available without Neo4j in this path.

---

## Primary Path: Docker Compose

```bash
git clone https://github.com/three-cubes/kairix && cd kairix
cp .env.example .env      # add your LLM API key
ln -s ~/my-notes ./documents
docker compose up -d
docker compose exec -it kairix kairix setup
```

**Prerequisite:** Docker Desktop (macOS/Windows) or Docker Engine (Linux). One-time admin install. Once Docker is installed, everything else runs without admin.

**What the user gets:**
- kairix MCP server (SSE on port 8080)
- kairix worker (hourly embed, entity seed)
- Neo4j (entity graph — people, companies, relationships)
- All three managed by Docker Compose — start, stop, logs, health checks

**User's documents:** Bind-mounted read-only from a user-chosen folder. Kairix never modifies them.

**Data paths (inside container):**

| Purpose | Container path | Host (Docker volume) |
|---------|---------------|---------------------|
| Documents | /data/vault (read-only) | ./documents (bind mount) |
| Database + vectors | /data/kairix/ | kairix-data volume |
| Neo4j | /data (Neo4j container) | neo4j-data volume |

**Agent connection:**

```json
{
  "mcp": {
    "servers": {
      "mcp-kairix": {
        "url": "http://localhost:8080"
      }
    }
  }
}
```

Works with OpenClaw (SSE), Claude Desktop (SSE), or any MCP-compatible agent.

---

## Fallback Path: pip install (no Docker)

```bash
pip install kairix
kairix setup
kairix search "your question"
```

No admin required. Runs as the installing user. Document permissions are automatic.

**Limitations vs Docker path:**
- No Neo4j — entity search (people, companies) not available
- No background worker — user must run `kairix embed` manually after adding documents
- No managed MCP server — user starts `kairix mcp serve` manually

**Data paths (user-level, all platforms):**

| Purpose | Linux/macOS | Windows |
|---------|-------------|---------|
| Config | ~/.config/kairix/ | %APPDATA%\kairix\ |
| Data (DB, vectors) | ~/.local/share/kairix/ | %LOCALAPPDATA%\kairix\ |
| Cache | ~/.cache/kairix/ | %LOCALAPPDATA%\kairix\cache\ |
| Reference library | ~/.local/share/kairix/reference-library/ | %LOCALAPPDATA%\kairix\reference-library\ |

---

## Server Deployment (Linux, always-on)

For production scenarios where agents connect 24/7.

**Reference shape:**

```
Service account:  kairix (system user, nologin, docker group)
Application:      /opt/kairix/app/                  (deployment-chosen)
Config:           /etc/kairix/.env                  (deployment-chosen)
Data:             /var/lib/kairix/ (Docker volumes) (deployment-chosen)
Documents:        bind mount, read-only (ACL or group read)
MCP:              SSE on 127.0.0.1:8080
Managed by:       systemd (kairix.service runs docker compose)
```

Paths shown are a reference shape; operators may relocate per their distribution's FHS conventions. The key invariants are:

- A dedicated service account with docker group membership and no login shell.
- Application code, config, and data on separate directories with appropriate ownership.
- MCP bound to loopback unless a reverse proxy with authentication fronts it.
- A process supervisor (systemd is the canonical choice on most distros) holding the lifecycle.

Requires admin to set up. Expected for infrastructure.

---

## Setup Wizard

Same wizard for all paths. Detects context (Docker vs pip, Neo4j available vs not).

```
Step 1: LLM Provider (Azure OpenAI / OpenAI / Other)
Step 2: Document Store (detect Obsidian vaults, offer reference library)
Step 3: Search Configuration (template: consulting / technical / general)
Step 4: Initial Index (scan, build FTS, embed)
```

If Neo4j detected: entity graph enabled automatically.
If not: skipped with note that entity search requires Neo4j.

---

## Consequences

- Docker Compose is the recommended path in all docs and the README
- pip is documented as "Without Docker" alternative
- Neo4j is included by default (Docker) — users never configure it manually
- Server deployment is a separate section for sysadmins
- The setup wizard adapts to whatever context it finds
