# kairix-memory-prompt — openclaw plugin

Appends the **kairix bootstrap envelope** (agent role, current Board.md, recent
memory files, active goals, health snapshot) to the agent's system prompt at
session start. Without this plugin, openclaw agents start each session
context-blind and react to user prompts instead of orienting themselves.

This plugin ships with kairix (`kairix.plugins.openclaw.memory-prompt`) and is
installed at:

- **In the kairix container image:** `/opt/kairix/plugins/openclaw/memory-prompt/`
- **From `pip install kairix`:** `<site-packages>/kairix/plugins/openclaw/memory-prompt/`

Tracked in [#246 W5](https://github.com/three-cubes/kairix/issues/246). Closes
the "plugin exists at a workspace-local path but isn't loaded" failure mode by
moving the plugin into the kairix release artefact.

---

## What the plugin does

1. Reads the agent name from openclaw's plugin context (`context.agent_name`).
2. Shells out to `kairix bootstrap <agent>` (the CLI shipped in #246 W1) with a
   5-second timeout.
3. Captures stdout (structured markdown — role, board, recent memory, active
   goals, health snapshot, next action).
4. Calls `context.appendSystemContext(markdown)` — **append, not replace** —
   so other openclaw plugins and the base agent prompt are preserved.

## Failure mode (degraded != broken)

If any step fails — the `kairix` binary is not on PATH, bootstrap exits
non-zero, the subprocess times out, openclaw passed a blank agent name, or
stdout came back empty — the plugin appends a short fallback string instead:

```
[kairix bootstrap unavailable — ask your admin to run kairix onboard check]
```

The session **starts normally**. The agent reads the fallback in its system
context, knows kairix orientation is unavailable, and surfaces that to the user
on its first reply. Per [#246](https://github.com/three-cubes/kairix/issues/246)
affordance contract: every degraded path tells the agent what to do next.

---

## openclaw config snippet

Paste this into `~/.openclaw/openclaw.json` (or wherever your openclaw config
lives — on the VM image, the canonical path is `/etc/openclaw/openclaw.json`):

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

Three keys, all required:

| Key | Why it matters |
|---|---|
| `plugins.load.paths` | Tells openclaw which directories to scan for plugins. Point it at `/opt/kairix/plugins/openclaw` and openclaw discovers every kairix-shipped plugin there in one line. |
| `plugins.allow` | Allowlist — even discovered plugins do not load unless they appear here. Defence in depth against accidental load of a stale or experimental plugin dropped into the path. |
| `plugins.entries.kairix-memory-prompt.hooks.allowPromptInjection` | Grants the plugin permission to call `appendSystemContext`. Without this, openclaw discovers the plugin but refuses to let it modify the system prompt. |

After editing, restart openclaw. You should see a line in the openclaw startup
log along the lines of:

```
[openclaw] loaded plugin: kairix-memory-prompt (hook: onSessionStart)
```

If you do not see that line, the plugin did not load — check `plugins.allow`
contains the literal string `kairix-memory-prompt` and that the directory at
`plugins.load.paths` actually contains a `kairix-memory-prompt/plugin.json`.

---

## Verifying it loaded

After restarting openclaw, start a new agent session and ask the agent:

> What is your bootstrap envelope?

If the plugin loaded successfully the agent should respond with its current
role, board, and recent memory (sourced from the kairix bootstrap markdown).

If the agent responds with the fallback string
`[kairix bootstrap unavailable — ask your admin to run kairix onboard check]`,
the plugin loaded but `kairix bootstrap` failed. Run on the host:

```bash
kairix onboard check
kairix bootstrap <agent-name>
```

Both should exit cleanly. The most common cause is `kairix` not on the
openclaw process's `$PATH` — fix by ensuring `/opt/kairix/bin` (or wherever
the `kairix` wrapper lives) is in the openclaw user's PATH.

---

## openclaw plugin API — documented assumptions

The plugin uses **two** entries on openclaw's plugin context object:

| Method / attribute | Contract |
|---|---|
| `context.agent_name` (attribute, `str`) | The active agent's slug. Mirrors the directory name under `04-Agent-Knowledge/<agent>/` in the kairix document store. |
| `context.appendSystemContext(text: str)` (method) | Appends `text` to the generated system prompt. Append, not replace; openclaw is responsible for ordering relative to other plugins. |

These names match the prior workspace-local plugin at
`/data/workspaces/builder/plugins/kairix-memory-prompt/` — the canonical move
into the repo does not change the openclaw-facing surface. **If the openclaw
build you are running renames either of these,** the plugin will fall back to
the degraded message (it never crashes the session) and you should file an
issue against this directory so the next release ships the corrected
attribute names.

The plugin manifest (`plugin.json`) declares:

| Field | Value | Why |
|---|---|---|
| `name` | `"kairix-memory-prompt"` | The string admins put in `plugins.allow`. |
| `runtime` | `"python"` | Tells openclaw to load the entry via Python (vs. node, wasm, etc). |
| `entry` | `"plugin.py"` | Relative path to the module that hosts the hook function. |
| `entryFunction` | `"on_session_start"` | The function openclaw imports and calls. |
| `hooks.onSessionStart` | `"on_session_start"` | Maps the canonical openclaw lifecycle event to the entry function. |
| `capabilities.promptInjection` | `"append"` | Declares the plugin only appends — does **not** replace. This is the configuration error that triggered #246. |

---

## Testing the plugin locally (without openclaw)

Smoke-test from a Python shell:

```python
from kairix.plugins.openclaw import memory_prompt_dir
import importlib.util

spec = importlib.util.spec_from_file_location(
    "memory_prompt_plugin",
    memory_prompt_dir() / "plugin.py",
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class FakeContext:
    agent_name = "alpha"

    def __init__(self) -> None:
        self.appended: list[str] = []

    def appendSystemContext(self, text: str) -> None:
        self.appended.append(text)


ctx = FakeContext()
mod.on_session_start(ctx)
print(ctx.appended[0][:200])
```

You should see the first 200 characters of the kairix bootstrap envelope for
agent `alpha`. If you see the fallback message, run `kairix bootstrap alpha`
in your shell and fix whatever the CLI reports.

The canonical test suite (mocked subprocess, no PATH dependency) lives at
`tests/plugins/test_kairix_memory_prompt.py`.

---

## Related

- [`docs/operations/MCP-DEPLOYMENT.md`](../../../../docs/operations/MCP-DEPLOYMENT.md) — full deploy story including this plugin
- [`docs/agents/ADMIN-CONVERSATION.md`](../../../../docs/agents/ADMIN-CONVERSATION.md) — the agent↔admin script when bootstrap is missing
- [`kairix/bootstrap_cli.py`](../../../bootstrap_cli.py) — the CLI this plugin shells out to (#246 W1)
