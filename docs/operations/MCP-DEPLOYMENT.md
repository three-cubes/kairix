# MCP Deployment

How to deploy the kairix MCP server in front of Claude Desktop, Claude Code, OpenClaw, or any other MCP-compatible client.

## Transport choices

Kairix supports three transports. Pick one per deployment:

| Transport | Endpoint | When to use |
|---|---|---|
| `stdio` | n/a | Claude Desktop / inline use. The kairix process is launched per-session by the MCP client; communication is via the process's stdin/stdout. |
| `http` (recommended for servers) | `POST /mcp` (streamable HTTP) and `GET/POST /sse` (legacy) on the same port | Server deployments — Claude Code over a tunnel, OpenClaw, anywhere a long-running MCP daemon makes sense. Each tool call is a normal HTTP request/response so reverse proxies and gateways treat it like any other API endpoint. |
| `sse` | `/sse` only | **Deprecated.** Kept as a `--transport=sse` alias for back-compat with existing scripts; emits a warning and acts as `http`. Migrate clients to `/mcp`. |

Streamable HTTP (the `/mcp` endpoint) is the recommended transport going forward. It's stateless per request, requires no session keep-alive, and survives gateway timeouts that historically broke `/sse` deployments. SSE remains mounted on the same port for any client that hasn't switched yet.

## Run

```bash
# Server deployments — recommended
kairix mcp serve --transport http --host 127.0.0.1 --port 8182

# Inline / Claude Desktop
kairix mcp serve --transport stdio
```

Flags:

- `--transport {stdio,http,sse}` — see table above.
- `--host` (default `127.0.0.1`) — bind address. **Do not bind to `0.0.0.0` without an authenticating gateway in front; the MCP server has no built-in authentication.**
- `--port` (default `8080`) — listening port. Auto-detected to a free port if the default is in use; set `KAIRIX_MCP_PORT` to make a substitution permanent.
- `--no-sse` — when `--transport=http`, omit the legacy `/sse` mount and serve only `/mcp`.

## Health

Kairix exposes two health endpoints. Use `/healthz` for liveness, `/healthz/ready` for layered readiness (v2026.5.10+).

### `/healthz` — basic liveness

```bash
curl http://127.0.0.1:8182/healthz
```

Returns `{"ready": true, "uptime_s": N}` once kairix has finished cold-starting (Neo4j driver, vector index, LLM clients). Tool calls before ready return a structured `{"error": "kairix-initializing", "retry_after_ms": 1500}` rather than crashing — clients can retry with backoff.

### `/healthz/ready` — layered readiness

```bash
curl http://127.0.0.1:8182/healthz/ready
```

Returns granular capability detail so a load balancer can distinguish "process up but degraded" from "fully operational":

```json
{
  "live": true,
  "ready": false,
  "uptime_s": 14,
  "checks": {
    "secrets_loaded": false,
    "vector_search_capable": false,
    "bm25_search_capable": true,
    "detail": {
      "secrets_loaded": "KAIRIX_LLM_API_KEY missing",
      "vector_search_capable": "embed credentials unavailable"
    }
  }
}
```

`ready` is the boolean to act on. The capability flags use the suffixes `_capable` (functional) and `_loaded` (configured). The `detail` map carries an actionable failure reason for any False capability. HTTP status is always 200 — load-balancer probes should treat the JSON body as the gate, not the status code.

Resolves the #167 gap where `/healthz` reported `ready=true` while vector search was silently broken because `/run/secrets/kairix.env` had never been hydrated after a reboot.

## Error envelope

Every tool handler is wrapped with `wrap_tool_errors`. Any exception escaping a handler becomes a structured response:

```json
{"error": "<ExceptionClass>: <message>"}
```

Exception class names are preserved so observability can group by error type. There is no path through which a tool exception reaches FastMCP's generic `-32602 Invalid request parameters` mapper — clients that observe `-32602` are looking at a transport-level (parameter-validation) failure, not a tool-level one.

## Gateway routing

If a reverse proxy or zero-trust gateway sits in front of kairix, route the following paths through to the kairix container:

```
/mcp     → 127.0.0.1:8182 (POST + GET)
/sse     → 127.0.0.1:8182 (GET, legacy)
/healthz → 127.0.0.1:8182 (GET)
```

For Caddy:

```caddyfile
mcp.example.com {
    reverse_proxy /mcp* 127.0.0.1:8182
    reverse_proxy /sse* 127.0.0.1:8182
    reverse_proxy /healthz 127.0.0.1:8182
}
```

For Cloudflare Access tunneling, the `/mcp` endpoint is a normal POST endpoint — no SSE-specific config (idle timeouts, buffering disable) is required. SSE callers do still need streaming-safe routing if they're not migrating off `/sse` immediately.

## Observability

Each tool call writes one JSON line to the search log. Default location:

- Docker: `/data/kairix/logs/search.jsonl`
- Non-Docker: `~/.cache/kairix/logs/search.jsonl`

Schema:

```json
{
  "ts": 1709553600,
  "query_hash": "12-hex-chars",
  "intent": "semantic",
  "agent": "alpha",
  "scope": "shared+agent",
  "collections_searched": ["docs", "alpha-memory"],
  "bm25_count": 5,
  "vec_count": 5,
  "fused_count": 8,
  "vec_failed": false,
  "fallback_used": false,
  "total_tokens": 1834,
  "latency_ms": 142.7
}
```

Watch the `vec_failed` rate and the `intent` distribution as a per-deployment health signal. A spike in `vec_failed` typically indicates the sqlite-vec extension isn't loading; a spike in `latency_ms` typically points at Neo4j or the embedding service. The architecture doc `agent-memory-architecture-recommendation-2026-04-16.md` discusses how this log feeds into the multi-agent quality loop.

## Scope and the agent registry

For `scope=all-agents` and `scope=everything` to work, `kairix.config.yaml` must declare its agents:

```yaml
collections:
  shared:
    - name: docs
      path: docs
  agent_pattern: "{agent}-memory"

agents:
  - name: alpha
    write_path: agents/alpha/memory
  - name: beta
    write_path: agents/beta/memory
  - name: gamma
    read_only: true
```

Validate before deploying:

```bash
kairix config validate
```

This catches duplicate agent names, overlapping `write_path` values (a write-isolation hazard), unknown `retrieval_overrides` keys (silent typos that would otherwise be invisible), and `agent_pattern` strings that omit the `{agent}` placeholder. Wire it into the CI pre-deploy step.

## Wiring the kairix-memory-prompt plugin

The kairix MCP server exposes tools an agent *can* call. For an agent to be **oriented at session start** — to arrive with its role, current `Board.md`, recent memory, and active goals already in its system prompt — openclaw also needs to load the `kairix-memory-prompt` plugin. Without it, agents start each session context-blind and react to user prompts instead of orienting themselves.

The plugin ships with kairix (#246 W5) and lives in the container image at:

```
/opt/kairix/plugins/openclaw/memory-prompt/
├── plugin.py        # openclaw entry — calls kairix bootstrap <agent>, appends stdout to system prompt
├── plugin.json      # openclaw manifest (declares name=kairix-memory-prompt, append-only injection)
└── README.md        # operator-facing details + the openclaw plugin API assumptions
```

For non-Docker installs the same files land under `<site-packages>/kairix/plugins/openclaw/memory-prompt/`. The container image symlinks the canonical `/opt/kairix/plugins/openclaw` path at build time so admins paste a stable path into openclaw config regardless of which Python minor version site-packages lives under.

### openclaw config snippet

Paste into your openclaw config (`~/.openclaw/openclaw.json` for per-user, `/etc/openclaw/openclaw.json` on the VM image):

```json
{
  "plugins": {
    "load": {
      "paths": ["/opt/kairix/plugins/openclaw"]
    },
    "allow": ["kairix-memory-prompt"],
    "entries": {
      "kairix-memory-prompt": {
        "hooks": {
          "allowPromptInjection": true
        }
      }
    }
  }
}
```

All three keys are required: `plugins.load.paths` tells openclaw where to discover plugins, `plugins.allow` is the explicit allowlist (defence in depth against accidental loads), and `plugins.entries.kairix-memory-prompt.hooks.allowPromptInjection` grants the plugin permission to call `appendSystemContext`. Without that last key, openclaw discovers the plugin but refuses to let it modify the system prompt — which is the failure mode the original incident exposed.

### Verifying it loaded

After restarting openclaw, look at the startup log for a line like:

```
[openclaw] loaded plugin: kairix-memory-prompt (hook: onSessionStart)
```

If that line is missing, the plugin did not load — re-check `plugins.allow` for the literal string `kairix-memory-prompt` and confirm `/opt/kairix/plugins/openclaw/memory-prompt/plugin.json` exists on disk.

If the plugin loaded but the bootstrap envelope is missing from agent sessions, the runtime probably cannot find the `kairix` CLI. The plugin shells out to `kairix bootstrap <agent>` with a 5-second timeout; if the binary is not on the openclaw user's `$PATH` the plugin falls back to a short degraded message (`[kairix bootstrap unavailable — ask your admin to run kairix onboard check]`) and the session still starts. Fix by adding the kairix install dir to PATH for the openclaw service unit.

### Failure contract — degraded != broken

The plugin **never blocks session start**. On every failure path — missing binary, non-zero exit, timeout, blank agent name, empty stdout — it appends the fallback string above and returns normally. This matches the #246 affordance contract: the agent reads the fallback in its system prompt, knows kairix orientation is unavailable, and surfaces that to the user instead of silently failing. Full failure-mode notes are in `kairix/plugins/openclaw/memory-prompt/README.md`.
