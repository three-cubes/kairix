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

```bash
curl http://127.0.0.1:8182/healthz
```

Returns `{"ready": true, "uptime_s": N}` once kairix has finished cold-starting (Neo4j driver, vector index, LLM clients). Tool calls before ready return a structured `{"error": "kairix-initializing", "retry_after_ms": 1500}` rather than crashing — clients can retry with backoff.

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
