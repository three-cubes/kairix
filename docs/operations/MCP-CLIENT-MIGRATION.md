# Migrating MCP clients from SSE to streamable HTTP

If you talk to kairix over MCP — Claude Desktop, Claude Code, OpenClaw, a custom Python or Node client — this guide gets you onto the new streamable HTTP transport. The migration is almost always a one-line URL change.

## TL;DR

Change `/sse` to `/mcp` in your MCP client configuration. Nothing else.

```diff
- "url": "http://kairix.example.com/sse"
+ "url": "http://kairix.example.com/mcp"
```

That's the entire client-side migration. Same host, same port, same tools, same parameters.

> **You do NOT need to set up Cloudflare tunnels, Cloudflare Access, OAuth, JWT, or any other auth.** If your kairix is at `http://localhost:8090` (the standard TC deployment), it stays at `http://localhost:8090`. If your kairix sits behind a gateway someone else operates, that gateway's auth keeps working unchanged for `/mcp` exactly as it did for `/sse` — that's an *operator* concern, not yours. Anything in this doc about Cloudflare or Caddy is reference material for whoever runs the kairix server, not steps you need to take.

If you can't migrate yet, **the old `/sse` endpoint still works** — kairix mounts both. You can move at your own pace. New clients should target `/mcp`; old clients can stay on `/sse` until you have time.

## Why migrate

The old transport kept a long-lived SSE connection open between the gateway and kairix. Idle timeouts on the gateway dropped that connection silently, and every subsequent tool call returned `-32602 Invalid request parameters` even though the parameters were fine. If you saw that error in the 2026-05-02 dogfood, this is the fix.

Streamable HTTP makes each tool call a normal HTTP request/response. The gateway treats it like any other API endpoint — no idle connection to drop, no keep-alive to configure.

## What does NOT change

- The host and port — still whatever your kairix is listening on (e.g. `127.0.0.1:8090` or `your-mcp-host.example.com`).
- Tool names — `search`, `entity`, `prep`, `timeline`, `research`, `contradict`, `usage_guide`. All seven still there.
- Tool parameters — same JSON schema, same defaults.
- Authentication — if your gateway uses Cloudflare Access, OAuth, or anything else, that all carries over to `/mcp` unchanged.

## What DOES change

- The path: `/sse` → `/mcp`.
- The HTTP semantics: tool calls are POSTs, not SSE event subscriptions. Your client library handles this for you if it supports MCP protocol revision 2025-03-26 or later. Anything from `mcp` Python SDK ≥ 1.5 or `@modelcontextprotocol/sdk` ≥ 1.5 supports it.
- Error handling: real exception class names come through in the response (`{"error": "RuntimeError: ..."}`) instead of being masked as `-32602`. If your client filters on `-32602` for retry logic, update it.

## Per-client migration steps

### Claude Desktop

1. Open `claude_desktop_config.json` (path varies by OS — see Claude Desktop's "Open config file" menu item).
2. Find your kairix MCP server entry. It probably looks like:
   ```json
   {
     "mcpServers": {
       "kairix": {
         "url": "http://localhost:8090/sse"
       }
     }
   }
   ```
3. Change `/sse` to `/mcp`:
   ```json
   {
     "mcpServers": {
       "kairix": {
         "url": "http://localhost:8090/mcp"
       }
     }
   }
   ```
4. Quit Claude Desktop fully and reopen. The new transport is in use on the next message.

### Claude Code

1. Open `.claude/mcp.json` in your repo (or `~/.claude/mcp.json` for user-level config).
2. Find the kairix entry and update the `url` field as above (`/sse` → `/mcp`).
3. Restart Claude Code.

### OpenClaw

1. Edit `/opt/openclaw/config/openclaw.json` (or wherever your OpenClaw config lives).
2. In each agent's `mcp` block, find:
   ```json
   "kairix": {
     "transport": "sse",
     "url": "http://localhost:8090/sse"
   }
   ```
   and change to:
   ```json
   "kairix": {
     "transport": "streamable-http",
     "url": "http://localhost:8090/mcp"
   }
   ```
3. Reapply the config: `sudo /opt/openclaw/scripts/apply-openclaw-config.sh`.
4. The agents pick up the new transport on their next session.

### Custom Python client

If you used `mcp.client.sse.sse_client(...)`:

```python
# Before
from mcp.client.sse import sse_client
async with sse_client("http://localhost:8090/sse") as (read, write):
    ...
```

Change to `streamablehttp_client`:

```python
# After
from mcp.client.streamable_http import streamablehttp_client
async with streamablehttp_client("http://localhost:8090/mcp") as (read, write, _):
    ...
```

Bump your `mcp` package floor to `>=1.20,<2`. The `streamablehttp_client` API has been stable since `mcp` 1.5 but 1.20+ has six months of stability fixes.

### Custom Node / TypeScript client

If you used `SSEClientTransport`:

```ts
// Before
import { SSEClientTransport } from "@modelcontextprotocol/sdk/client/sse.js";
const transport = new SSEClientTransport(new URL("http://localhost:8090/sse"));
```

Change to `StreamableHTTPClientTransport`:

```ts
// After
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
const transport = new StreamableHTTPClientTransport(new URL("http://localhost:8090/mcp"));
```

Bump `@modelcontextprotocol/sdk` to a version that includes `streamableHttp.js` (1.5+).

## Verifying the migration worked

After updating your client and restarting, run any tool call you'd run normally. If results come back, you're on the new transport.

If you want a deliberate end-to-end check before trusting it:

```bash
# Check kairix is healthy
curl http://localhost:8090/healthz
# Expect: {"ready":true,"uptime_s":N}

# Check tools/list works on /mcp
curl -X POST http://localhost:8090/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
# Expect: a JSON-RPC response listing seven tools
```

If both succeed, the server side is good. Any client-side issue is in your MCP client configuration.

## Common errors and fixes (client-side)

| What you see | What it means | Fix |
|---|---|---|
| `-32602 Invalid request parameters` on every tool call | You're hitting `/sse` and the connection between your client and kairix dropped while idle | Change `/sse` to `/mcp` in your client config. |
| `404 Not Found` when you POST to `/mcp` | The kairix you're hitting is older than v2026.5.3 — `/mcp` doesn't exist yet | Either ask the operator to upgrade kairix, or stay on `/sse` for now. |
| Tool calls silently hang | Your MCP client library is too old for streamable HTTP | Update: Python `mcp>=1.20`, Node `@modelcontextprotocol/sdk>=1.5`. |
| `connection refused` | kairix isn't running, or you're hitting the wrong host/port | Ask the operator to confirm `kairix mcp serve` is running and `curl http://<host>/healthz` returns ready. |

If you're seeing something not in this table, capture the exact error and ping the operator — it's almost certainly an operator-side issue, not your client config.

## Rollback

If migration breaks something for you and you can't debug it right now: change `/mcp` back to `/sse` in your client config. The legacy endpoint stays mounted in kairix. You're back to the pre-migration behaviour, and you can investigate at your own pace.

There's no kairix-side rollback to perform — the server is happy to serve both paths simultaneously.

## When `/sse` will go away

Not in v2026.5.3. The deprecation warning starts appearing in kairix server logs when an operator runs `--transport=sse` on the CLI; the SSE endpoint itself stays available as long as clients are using it. We'll announce a removal target after most clients have migrated. For now, we want migrations to be unhurried — a year-long "remove `/sse`" plan is fine.
