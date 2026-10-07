"""
kairix.agents.mcp.cli — CLI entry point for the MCP server.

Usage:
    kairix mcp serve [--port PORT] [--transport stdio|http|sse] [--no-sse]

Transports:
    stdio — for Claude Desktop / inline use (default).
    http  — uvicorn-served streamable HTTP at /mcp (recommended for server
            deployments). Also mounts /sse for back-compat unless --no-sse.
    sse   — deprecated alias for http (kept for back-compat with existing
            scripts; emits a deprecation warning).

Requires kairix[agents]: pip install 'kairix[agents]'
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from kairix.platform.onboard.startup_preflight import StartupCredentialFailure

from kairix.agents.mcp.cold_start import warm_retrieval_stack
from kairix.platform.onboard.ports import find_available_port, is_port_available

# Dedicated startup-event logger. Operators filter on this name in log
# analytics to pivot on container/process restart frequency. See
# ``docs/operations/MCP-DEPLOYMENT.md`` ("Cold-start affordance contract")
# for the event vocabulary and the operator-side query recipe.
_STARTUP_LOGGER_NAME = "kairix.mcp.startup"
startup_logger = logging.getLogger(_STARTUP_LOGGER_NAME)

# Structured-log event names. Strings live as module constants so log
# analytics dashboards have a single grep target and F17 (no triple-duplicated
# literals) stays clean.
_EVENT_PROCESS_STARTED = "mcp_process_started"
_EVENT_WARM_STARTED = "mcp_warm_started"
_EVENT_WARM_FAILED = "mcp_warm_failed"

# Warm-result envelope key — duplicated across the structured-log emit
# and the human-readable startup message; F17-clean via single constant.
_KEY_ELAPSED_MS = "elapsed_ms"


def _format_event(event: str, fields: dict[str, Any]) -> str:
    """Render a structured startup-log line.

    Output shape: ``event=<name> key1=<value1> key2=<value2> ...``. Values
    are JSON-encoded when they contain whitespace or aren't a primitive so
    a downstream log analytics layer can re-parse the line. Plain scalars
    are written without quotes to stay grep-friendly for operators reading
    raw ``docker logs`` output.
    """
    parts = [f"event={event}"]
    parts.extend(f"{key}={_render_event_value(value)}" for key, value in fields.items())
    return " ".join(parts)


def _render_event_value(value: Any) -> str:
    """Render one ``key=value`` value for :func:`_format_event`.

    ``None`` / bools become ``null`` / ``true`` / ``false``, numbers and
    quote-free single-word strings are written bare, everything else is
    JSON-encoded so it stays re-parseable.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str) and " " not in value and '"' not in value:
        return value
    return json.dumps(value)


def _default_warm_flag_path() -> Path:
    """Production seam — lazy-import warm_flag_path so it isn't loaded at module import."""
    from kairix.paths import warm_flag_path

    return warm_flag_path()


def _previous_warm_age_seconds(warm_flag_path_fn: Callable[[], Path]) -> float | None:
    """Read the warm-flag mtime and return ``now - mtime`` in seconds.

    Returns ``None`` on first start (flag absent) or unreadable flag.
    Operators use this to disambiguate ``container restarted while warm``
    from ``container restarted while still cold`` in their log analytics.

    ``warm_flag_path_fn`` is the public DI seam — tests inject a tmp_path
    fake so F2 (no ``monkeypatch.setenv("KAIRIX_WARM_FLAG_PATH", ...)``)
    stays clean.
    """
    try:
        flag = warm_flag_path_fn()
        if not flag.exists():
            return None
        return round(time.time() - flag.stat().st_mtime, 1)
    except OSError:
        return None


def _emit_process_started(host: str, port: int, *, previous_warm_age_s: float | None) -> None:
    """Emit the ``mcp_process_started`` structured log event.

    Fired once per process at the top of the HTTP-transport branch — before
    warm-up runs. Operators pivot on a count over time to see container
    restart frequency; ``previous_warm_age_s`` answers ``was the previous
    process warm when it died?``.

    ``previous_warm_age_s`` is captured by the caller before
    :func:`kairix.platform.warm.state.reset_warm_state` unlinks the
    cross-process warm flag — the caller owns ordering so the flag-age
    signal survives the reset.
    """
    from kairix import __version__ as kairix_version

    startup_logger.info(
        _format_event(
            _EVENT_PROCESS_STARTED,
            {
                "pid": os.getpid(),
                "host": host,
                "port": port,
                "python_version": platform.python_version(),
                "kairix_version": kairix_version,
                "previous_warm_age_s": previous_warm_age_s,
            },
        )
    )


def _emit_warm_outcome(warm_result: dict[str, Any]) -> None:
    """Emit ``mcp_warm_started`` (success) or ``mcp_warm_failed`` (not ready).

    Operators correlate this with ``mcp_process_started`` by ``pid`` to see
    end-to-end warm-up wall-clock per restart and which warm steps failed
    when ready is False.
    """
    if warm_result.get("ready") is True:
        startup_logger.info(
            _format_event(
                _EVENT_WARM_STARTED,
                {
                    "pid": os.getpid(),
                    _KEY_ELAPSED_MS: warm_result.get(_KEY_ELAPSED_MS, 0),
                },
            )
        )
        return
    startup_logger.info(
        _format_event(
            _EVENT_WARM_FAILED,
            {
                "pid": os.getpid(),
                "warm_result": warm_result,
            },
        )
    )


def _default_credential_preflight(env: Mapping[str, str]) -> list[StartupCredentialFailure]:
    """Production seam — lazy-import the startup credential preflight (#449).

    Returns the severity-tagged credential problems found in *env* (warn
    for a missing/placeholder LLM key, fatal for an empty neo4j password
    when a URI is configured). Lazy-imported so the onboard module's
    transitive deps aren't loaded at CLI module import.
    """
    from kairix.platform.onboard.startup_preflight import preflight_startup_credentials

    return preflight_startup_credentials(env)


def _default_build_server() -> Callable[..., Any]:
    """Default factory: lazy-import build_server so it isn't loaded at module import."""
    from kairix.agents.mcp.server import build_server

    return build_server


def _default_uvicorn_run() -> Callable[..., Any]:
    """Default factory: lazy-import uvicorn.run so it isn't loaded at module import."""
    import uvicorn

    return cast("Callable[..., Any]", uvicorn.run)


def _default_warm_runner(warm_body: Callable[[], None]) -> None:
    """Production seam — spawn ``warm_body`` in a daemon thread so the
    main thread proceeds to ``uvicorn.run`` without blocking on warm.

    The thread is named so operators can see it in ``threading.enumerate()``
    or a pyspy dump. Daemon=True so a SIGTERM to the worker doesn't hang
    waiting for warm to complete. ColdStartMiddleware in transport.py
    returns the structured 503 + ColdStart envelope for every call
    until ``gate.mark_ready()`` fires from the warm thread.

    Per #320: this replaces the pre-fix synchronous warm that blocked
    ``main()`` for 7-30s before uvicorn bound the port — during which
    MCP clients saw the opaque OS-level "fetch failed" string instead
    of the F21-compliant ColdStart affordance.
    """
    import threading

    thread = threading.Thread(target=warm_body, daemon=True, name="kairix-mcp-warm")
    thread.start()


def _default_operator_token() -> str | None:
    """Resolve the operator token via the production SecretsLoader (#500).

    Returns the configured ``kairix-infra-operator-token`` value, or
    ``None`` when no token is set (loopback-only / pip-install case where
    the tokened URL is irrelevant). The value is a live credential — its
    only sanctioned print surface is the first-boot onboarding line in
    ``_cmd_serve`` (the Jupyter precedent); it is never logged elsewhere
    (F15). Lazy-imported so the resolver isn't constructed at module load.
    """
    from kairix.secrets.loader import SecretsLoader

    return SecretsLoader().get("infra", "operator", None, "token")


@dataclass
class McpCliDeps:
    """Injection seam for the MCP CLI so tests can drive it without binding ports.

    Production callers leave this None — ``main()`` constructs the default
    Deps which lazily resolves the real ``build_server`` and ``uvicorn.run``.
    Tests pass a Deps with fakes that record their invocations instead of
    starting servers.

    Port-probe seams (``is_port_available_fn`` / ``find_available_port_fn``)
    let tests drive ``_resolve_port`` without monkey-patching
    ``kairix.platform.onboard.ports``. Production defaults call through to
    the real functions; tests inject fakes that pin port-conflict scenarios.
    """

    build_server_factory: Callable[[], Callable[..., Any]] = field(default_factory=lambda: _default_build_server)
    uvicorn_runner_factory: Callable[[], Callable[..., Any]] = field(default_factory=lambda: _default_uvicorn_run)
    is_port_available_fn: Callable[[int], bool] = field(default_factory=lambda: is_port_available)
    find_available_port_fn: Callable[..., int] = field(default_factory=lambda: find_available_port)
    warm_retrieval_stack_fn: Callable[[], dict[str, Any]] = warm_retrieval_stack
    warm_flag_path_fn: Callable[[], Path] = field(default_factory=lambda: _default_warm_flag_path)
    # #320 — pluggable warm-execution strategy. Production default spawns
    # warm in a daemon thread so uvicorn binds the port immediately.
    # Tests pass ``lambda fn: fn()`` to run warm synchronously when they
    # want deterministic stderr / readiness ordering (e.g. existing
    # warm-up-complete assertions).
    warm_runner: Callable[[Callable[[], None]], None] = field(default_factory=lambda: _default_warm_runner)
    # #449 — boot-time credential preflight + exit + env seams. The
    # preflight warns (degrade to search-only) on a missing/placeholder
    # LLM key and hard-exits on an empty neo4j password when a URI is set.
    # ``serve_env`` defaults to the resolved process env (after
    # bootstrap_secrets ran in main()); tests inject an explicit mapping so
    # no os.environ mutation is needed (F2-clean). ``exit_fn`` defaults to
    # sys.exit so the fatal branch is testable without killing the process.
    credential_preflight_fn: Callable[[Mapping[str, str]], list[StartupCredentialFailure]] = field(
        default_factory=lambda: _default_credential_preflight
    )
    serve_env: Mapping[str, str] = field(default_factory=lambda: os.environ)
    exit_fn: Callable[[int], None] = field(default_factory=lambda: cast("Callable[[int], None]", sys.exit))
    # #500 — tokened wizard-URL boot print. Production defaults resolve the
    # operator token through its real seam; tests inject a fake (F2-clean —
    # no KAIRIX_INFRA_OPERATOR_TOKEN setenv). The wizard is always mounted
    # (the setup_wizard_web cutover flag retired, PLA-287), so the print is
    # gated only on an operator token being configured.
    operator_token_fn: Callable[[], str | None] = field(default_factory=lambda: _default_operator_token)


def main(argv: list[str] | None = None, *, deps: McpCliDeps | None = None) -> None:
    # Hydrate the secrets bundle into os.environ ONCE at MCP-server
    # boot, before any SecretsLoader (or any credential-reading code
    # path) is constructed. See kairix/secrets/bootstrap.py for why
    # this lives here (and at every other entry point) rather than
    # inside SecretsLoader.__init__.
    from kairix.secrets.bootstrap import bootstrap_secrets

    bootstrap_secrets()

    parser = argparse.ArgumentParser(
        prog="kairix mcp",
        description="MCP server: expose search/entity/prep/timeline as MCP tools",
    )
    sub = parser.add_subparsers(dest="subcommand")

    serve_p = sub.add_parser("serve", help="Start the MCP server")
    serve_p.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port to listen on for http/sse transport (default: 8080)",
    )
    serve_p.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind to for http/sse transport (default: 127.0.0.1). "
        "WARNING: The MCP server has no authentication. Do not bind to 0.0.0.0 "
        "unless you have network-level access controls in place.",
    )
    serve_p.add_argument(
        "--transport",
        choices=["stdio", "http", "sse"],
        default="stdio",
        help="Transport: stdio (default, for Claude Desktop), http (streamable "
        "HTTP at /mcp + legacy /sse), or sse (alias for http; see module docstring)",
    )
    serve_p.add_argument(
        "--no-sse",
        action="store_true",
        help="When --transport=http, omit the legacy /sse mount and serve only /mcp",
    )

    args = parser.parse_args(argv)

    effective_deps = deps or McpCliDeps()
    if args.subcommand == "serve":
        _cmd_serve(args, deps=effective_deps)
    else:
        parser.print_help()
        sys.exit(1)


def _resolve_port(args: argparse.Namespace, *, deps: McpCliDeps) -> int:
    """Resolve MCP port: CLI flag → env var → config → auto-detect.

    The auto-detect path uses ``deps.is_port_available_fn`` /
    ``find_available_port_fn`` — production callers leave deps at the
    default; tests inject fakes via the McpCliDeps DI seam.
    """
    from kairix.paths import mcp_port_raw

    # CLI flag takes precedence (argparse default is 8080)
    if "--port" in sys.argv:
        return int(args.port)

    # Environment variable (env read lives in kairix.paths — F4)
    env_port = mcp_port_raw()
    if env_port:
        return int(env_port)

    # Auto-detect: check if default port is available
    default = 8080
    if deps.is_port_available_fn(default):
        return default

    suggested = deps.find_available_port_fn(preferred=default)
    print(
        f"Port {default} is in use — using {suggested} instead. "
        f"Set KAIRIX_MCP_PORT={suggested} to make this permanent.",
        file=sys.stderr,
    )
    return suggested


def _preflight_or_warn(*, deps: McpCliDeps) -> bool:
    """Run the boot-time credential preflight (#449): warn, or exit on fatal.

    For every ``warn`` failure (missing/placeholder LLM key) log a WARNING
    naming the VAR (never the value — F15) with the F21-style affordance,
    then keep going — BM25 keyword search works without an LLM key, so a
    search-only operator must still be able to boot.

    For any ``fatal`` failure (empty neo4j password when a URI is set)
    print the affordance block to stderr and call ``deps.exit_fn(1)`` —
    the graph layer genuinely can't run.

    Returns ``True`` when a fatal failure was seen (so the caller stops
    before constructing the server). The production ``exit_fn`` (``sys.exit``)
    raises ``SystemExit`` and never returns; a test's fake ``exit_fn`` may
    return, so the boolean lets ``_cmd_serve`` stop deterministically in
    both cases.

    The env is read from ``deps.serve_env`` (the resolved process env after
    ``bootstrap_secrets`` ran in ``main()``); tests inject an explicit
    mapping so the placeholder / fatal branches are exercised without
    mutating ``os.environ``.
    """
    failures = deps.credential_preflight_fn(deps.serve_env)
    fatal_seen = False
    for failure in failures:
        if failure.severity == "fatal":
            fatal_seen = True
            print(
                f"ERROR: {failure.reason}\n  {failure.fix}",
                file=sys.stderr,
            )
        else:
            startup_logger.warning("%s %s", failure.reason, failure.fix)
    if fatal_seen:
        deps.exit_fn(1)
    return fatal_seen


def _maybe_print_wizard_url(*, port: int, deps: McpCliDeps) -> None:
    """Print the one-time tokened wizard URL when the wizard is reachable (#500).

    The wizard is always mounted (the ``setup_wizard_web`` cutover flag
    retired, PLA-287), so this fires whenever an operator token is
    configured — exactly the case where a browser needs the tokened URL to
    set its grant cookie (stock Docker presents every published-port peer
    as the bridge IP, never loopback). Loopback / pip-install operators
    reach the wizard header-free and see no print.

    This is the sanctioned onboarding surface for the token (the Jupyter
    precedent): it goes to stderr so ``docker compose logs`` surfaces it,
    and it is the ONLY place the token is emitted — no app-level logger
    ever sees it (F15).
    """
    token = deps.operator_token_fn()
    if not token:
        return
    from kairix.paths import wizard_tokened_url

    url = wizard_tokened_url(token=token, port=port)
    print(
        f"Setup wizard remote access: open this one-time URL in a browser to grant access\n  {url}",
        file=sys.stderr,
    )


def _cmd_serve(args: argparse.Namespace, *, deps: McpCliDeps) -> None:
    # #449 — boot-time credential preflight. Warn-and-degrade to search-only
    # on a missing/placeholder LLM key (BM25 still works); hard-exit on an
    # empty neo4j password when a URI is configured. Runs first so the
    # operator sees the actionable message before any server construction.
    if _preflight_or_warn(deps=deps):
        return

    # Capture the previous warm-flag age BEFORE reset_warm_state unlinks
    # it — Part 3 of KFEAT-020 needs this for the mcp_process_started log
    # event so operators can tell whether the just-killed previous process
    # was warm at death.
    previous_warm_age_s = _previous_warm_age_seconds(deps.warm_flag_path_fn)

    # Clear any stale warm flag from a previous MCP process. The flag lives
    # on the persistent kairix data volume so it survives container restarts
    # by default; the in-process warm state, however, is reset on each
    # start. Clearing here keeps the docker healthcheck honest: a freshly
    # started container reports not-ready until it actually re-warms.
    from kairix.platform.warm.state import reset_warm_state

    reset_warm_state()

    try:
        build_server = deps.build_server_factory()
    except ImportError:
        print(
            "Error: MCP dependencies not installed. Run: pip install 'kairix[agents]'",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.transport == "sse":
        print(
            "WARNING: --transport=sse is deprecated; use --transport=http "
            "(serves both /mcp and /sse). Continuing as http.",
            file=sys.stderr,
        )
        args.transport = "http"

    if args.transport == "stdio":
        server = build_server(host=args.host, port=args.port)
        print("Starting kairix MCP server (stdio transport)", file=sys.stderr)
        server.run(transport="stdio")
        return

    # http transport — streamable HTTP at /mcp via uvicorn, optional /sse legacy
    port = _resolve_port(args, deps=deps)

    # Emit ``mcp_process_started`` BEFORE warm-up so operators can pivot on
    # process-start cadence even when warm-up never completes. The matching
    # ``mcp_warm_started`` / ``mcp_warm_failed`` event below carries the
    # warm outcome — ``docs/operations/MCP-DEPLOYMENT.md`` documents the
    # event vocabulary and the log-analytics query recipe.
    _emit_process_started(args.host, port, previous_warm_age_s=previous_warm_age_s)

    from kairix.agents.mcp.capability_probe import build_capability_probe
    from kairix.agents.mcp.readiness import EventReadinessGate
    from kairix.agents.mcp.transport import build_mcp_app

    # HTTP deployments are long-running shared services. Pay the expensive
    # retrieval initialisation cost before advertising readiness so the first
    # user-facing tool call does not receive a cold-start surprise.
    gate = EventReadinessGate()
    server = build_server(host=args.host, port=port, readiness_check=gate.is_ready, mark_ready=gate.mark_ready)
    capability_probe = build_capability_probe()
    app = build_mcp_app(
        server,
        with_sse=not args.no_sse,
        readiness_check=gate.is_ready,
        capability_probe=capability_probe,
    )

    # #320 — warm runs in a background thread so uvicorn binds the port
    # FIRST. Previously warm ran synchronously here (~7-30s blocking),
    # then uvicorn.run was called — so during the warm window the port
    # wasn't open and MCP clients got connection-refused at the OS
    # network layer, which JS fetch() reports as the opaque string
    # "fetch failed". With warm in the background, the port binds
    # immediately and ColdStartMiddleware (transport.py) returns a
    # structured 503 + ColdStart envelope for every call during warm —
    # agents get the affordance, not an opaque failure.
    #
    # #390 — register a live WarmProgress snapshot at warm-start so the
    # ColdStart envelope reports the actual remaining time (~120s on a
    # fresh container, dominated by the vec_index cold-load) instead of
    # the historical static 8s that caused agents to back off 8s x N
    # retries and thrash.
    from kairix.platform.warm.state import WarmProgress, set_warm_progress

    set_warm_progress(WarmProgress(started_at=time.time()))

    def _warm_and_mark_ready() -> None:
        warm_result = deps.warm_retrieval_stack_fn()
        _emit_warm_outcome(warm_result)
        if warm_result.get("ready") is True:
            gate.mark_ready()
            # #390 — clear the WarmProgress holder once ready so any
            # late-arriving ColdStart envelope (a request that races the
            # gate flip) falls back cleanly rather than reporting a
            # negative remaining-seconds value.
            set_warm_progress(None)
            # GH #355 — also flip the cross-process warm flag so the docker
            # healthcheck (kairix onboard ready) and the ColdStartMiddleware
            # see the same warm signal. Before this call, the MCP server's
            # in-process ``gate`` flipped to ready but the warm-flag file
            # at warm_flag_path() never appeared, so ``docker ps`` showed
            # the container as ``unhealthy`` for the process lifetime even
            # after warm-up completed in the application logs. Threading
            # the deps-injected ``warm_flag_path_fn()`` through ``mark_warm``
            # keeps the test seam intact (F2-clean — no env monkeypatch).
            from kairix.platform.warm.state import mark_warm

            mark_warm(flag_path=deps.warm_flag_path_fn())
            print(
                f"warm-up complete — elapsed_ms={warm_result.get(_KEY_ELAPSED_MS, 'unknown')}",
                file=sys.stderr,
            )
        else:
            print(
                f"WARNING: kairix warm-up incomplete; tools will return KAIRIX_COLD_START until ready: {warm_result}",
                file=sys.stderr,
            )

    deps.warm_runner(_warm_and_mark_ready)

    sse_status = "+ /sse legacy" if not args.no_sse else "(no /sse)"
    print(
        f"Starting kairix MCP server on http://{args.host}:{port}/mcp {sse_status}",
        file=sys.stderr,
    )
    _maybe_print_wizard_url(port=port, deps=deps)

    uvicorn_run = deps.uvicorn_runner_factory()
    # R1 (#387) — pin workers=1 so we never accidentally duplicate the
    # 10.86 GB vec_index mmap + per-worker pipeline cache. The MCP server
    # holds expensive process-local state (vec_index, EmbedCache,
    # QueryResultCache, TopologyCollectionResolver SQLite handle); a
    # second worker would re-load all of it and fragment cache hits.
    # ``loop="uvloop"`` is preferred for ~10-20% async-overhead reduction
    # under load; falls back to asyncio default if uvloop isn't installed
    # (uvloop is optional — not yet a hard dep, see #387 follow-up).
    loop_kind: str = "auto"
    try:
        import uvloop as _uvloop  # noqa: F401 — probe-import for availability

        loop_kind = "uvloop"
    except ImportError:
        loop_kind = "auto"
    # R8 (#395) — explicit uvicorn timeouts for long MCP tool calls.
    # Production trace 2026-06-03 showed ASGI ResponseTimeout firing on
    # long-running ``brief`` / ``prep`` tool calls — uvicorn's default
    # ``timeout_keep_alive=5s`` was closing the keep-alive socket
    # mid-streaming-response, surfacing as a client-side "error" even
    # though the server-side tool work completed successfully.
    # ``timeout_graceful_shutdown=60`` lets in-flight tool calls drain
    # cleanly on SIGTERM (systemd ``ExecStop``) instead of being cut
    # off at the default 0s drain window.
    uvicorn_run(
        app,
        host=args.host,
        port=port,
        log_level="info",
        workers=1,
        loop=loop_kind,
        timeout_keep_alive=120,
        timeout_graceful_shutdown=60,
    )
