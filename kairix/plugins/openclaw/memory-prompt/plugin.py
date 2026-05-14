"""kairix-memory-prompt — openclaw plugin entry point (#246 W5).

What this plugin does
=====================

At openclaw session start, this plugin shells out to ``kairix bootstrap
<agent>`` (the CLI shipped in #246 W1) and **appends** the resulting
markdown to the agent's system context via openclaw's
``appendSystemContext`` API.

The plugin is intentionally tiny: it does not embed kairix logic, does
not duplicate the bootstrap envelope, and does not hold a long-lived
connection. One subprocess call per session start. The full orientation
envelope (role / board / recent memory / health) is produced by the
``kairix bootstrap`` CLI; this plugin just delivers it into the agent's
system prompt.

Failure contract
================

**The plugin must not block session start under any failure mode.** If
``kairix bootstrap`` fails (binary missing, non-zero exit, timeout, or
the agent name cannot be resolved from openclaw context), the plugin
appends a *short* fallback message that tells the agent its context is
degraded and points the admin at ``kairix onboard check``. The session
starts; the agent runs reactively until the admin fixes the underlying
issue.

This is the affordance contract from #246: degraded != broken.

openclaw API surface (assumed)
==============================

The plugin uses two methods on the openclaw plugin context object:

- ``context.agent_name`` (attribute) — string slug for the active agent.
- ``context.appendSystemContext(text)`` (method) — appends ``text`` to
  the generated system prompt. Append, not replace; openclaw is
  responsible for ordering relative to other plugins.

These mirror the conventions used by the prior workspace-local plugin at
``/data/workspaces/builder/plugins/kairix-memory-prompt/``. If your
openclaw build renames either of these surfaces the plugin falls back
to its short degraded message — it never crashes session start. The
canonical assumptions list and how to file an issue against this
directory live in the plugin's ``README.md``.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# Hard cap on the bootstrap call. Bootstrap is filesystem-only and
# should return in well under a second; the budget here is defence in
# depth against a stalled disk or a misconfigured doc root. If the cap
# trips, the plugin falls back to the degraded message — never blocks
# the session.
BOOTSTRAP_TIMEOUT_S: float = 5.0

# Short, prescriptive fallback. Tells the agent context is degraded and
# tells the admin what to run. Matches the #246 affordance contract:
# every degraded path tells the agent what to do next.
FALLBACK_MESSAGE: str = "[kairix bootstrap unavailable — ask your admin to run kairix onboard check]"


class OpenclawContext(Protocol):
    """Minimal openclaw plugin context surface this plugin relies on.

    Defined as a Protocol so the plugin code is decoupled from the
    runtime: tests pass a fake (see ``tests/plugins/`` for the canonical
    shim) and openclaw passes its real context object. Either works.
    """

    agent_name: str

    def appendSystemContext(self, text: str) -> None:  # noqa: N802 — openclaw API name
        """Append ``text`` to the agent's generated system prompt."""
        ...


@dataclass(frozen=True)
class PluginDeps:
    """Injectable dependencies for :func:`on_session_start`.

    Tests pass a ``PluginDeps`` whose ``run_bootstrap`` returns a known
    string (or raises) so we can assert the plugin's behaviour without
    actually shelling out. Production callers leave ``deps=None`` and
    the defaults wire the real subprocess.
    """

    run_bootstrap: Callable[[str], str]
    """Run ``kairix bootstrap <agent>`` and return stdout markdown.

    Raises any exception on failure — the caller catches and falls back.
    """


def _default_run_bootstrap(agent: str) -> str:
    """Production wiring: ``subprocess.run(["kairix", "bootstrap", agent])``.

    Returns the captured stdout as a string. Raises ``RuntimeError`` on
    non-zero exit, missing binary, or timeout — the caller converts any
    raised exception into the fallback path.
    """
    binary = shutil.which("kairix")
    if binary is None:
        raise RuntimeError("kairix binary not on PATH")

    # The argv list is fully literal: we pass the resolved binary path
    # and the agent name (which originates from openclaw plugin
    # context, not user input). Trusted local CLI invocation.
    completed = subprocess.run(  # noqa: S603  # Trusted local CLI; argv is literal-plus-agent-slug.
        [binary, "bootstrap", agent],
        capture_output=True,
        text=True,
        timeout=BOOTSTRAP_TIMEOUT_S,
        check=False,
    )
    if completed.returncode != 0:
        # Promote the stderr tail into the exception so logs surface the
        # actual failure rather than a generic "non-zero exit".
        stderr_tail = (completed.stderr or "").strip().splitlines()[-3:]
        raise RuntimeError(
            f"kairix bootstrap exited {completed.returncode}: {' | '.join(stderr_tail) or '<no stderr>'}"
        )
    return completed.stdout


def _resolve_agent_name(context: Any) -> str:
    """Pull the agent name from openclaw context, normalised.

    Returns the empty string when the attribute is missing or blank;
    callers treat empty as a fallback trigger.
    """
    name = getattr(context, "agent_name", "") or ""
    return name.strip()


def on_session_start(context: OpenclawContext, *, deps: PluginDeps | None = None) -> None:
    """Openclaw plugin hook — invoked once per agent session start.

    Calls ``kairix bootstrap <agent>``, captures stdout, and appends it
    to the agent's system prompt via ``appendSystemContext``. On any
    failure (missing binary, non-zero exit, timeout, blank agent name)
    the plugin appends :data:`FALLBACK_MESSAGE` instead and returns
    normally — session start is never blocked.

    The ``deps`` parameter is the test seam. Production callers leave
    it ``None`` and the defaults wire the real subprocess.
    """
    effective_deps = deps if deps is not None else PluginDeps(run_bootstrap=_default_run_bootstrap)

    agent = _resolve_agent_name(context)
    if not agent:
        logger.warning("openclaw context did not supply agent_name; falling back")
        context.appendSystemContext(FALLBACK_MESSAGE)
        return

    try:
        markdown = effective_deps.run_bootstrap(agent)
    except Exception as exc:
        # Swallow every exception class — the contract is "never block
        # session start". The exception is logged for operator review.
        logger.warning("kairix bootstrap failed for agent %s: %s", agent, exc, exc_info=True)
        context.appendSystemContext(FALLBACK_MESSAGE)
        return

    if not markdown or not markdown.strip():
        # Defensive: a zero-byte stdout is functionally the same as a
        # failure for the agent reading the prompt. Use the fallback.
        logger.warning("kairix bootstrap returned empty stdout for agent %s; falling back", agent)
        context.appendSystemContext(FALLBACK_MESSAGE)
        return

    context.appendSystemContext(markdown)


__all__ = [
    "BOOTSTRAP_TIMEOUT_S",
    "FALLBACK_MESSAGE",
    "OpenclawContext",
    "PluginDeps",
    "on_session_start",
]
