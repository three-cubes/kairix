"""Unit tests for the MCP CLI.

The BDD CLI suite (``tests/bdd/test_mcp_cli.py``) covers ``--help`` /
no-args / invalid-transport paths. These unit tests cover the serve
runtime paths via the ``McpCliDeps`` injection seam.
"""

from __future__ import annotations

import io
import sys
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from typing import Any

import pytest

from kairix.agents.mcp.cli import McpCliDeps
from kairix.agents.mcp.cli import main as mcp_main

# F5 keeps tests off internal ``_x`` imports; these are the PUBLIC env-var
# names (the operator-facing contract). Production single-sources them from
# onboard.check's ``_CANONICAL_SECRETS`` (F85).
_LLM_API_KEY_VAR = "KAIRIX_PROVIDER_LLM_API_KEY"  # pragma: allowlist secret — env-var NAME, not a credential
_LLM_ENDPOINT_VAR = "KAIRIX_PROVIDER_LLM_ENDPOINT"

# #449 — a deterministic all-good env for the serve runtime-path tests:
# real-looking LLM creds (so the preflight doesn't warn) and a non-empty
# neo4j password with a URI set (so the preflight doesn't hard-exit).
_GOOD_SERVE_ENV = {
    _LLM_API_KEY_VAR: "sk-live-cli-unit-real-key",  # pragma: allowlist secret — fake fixture
    _LLM_ENDPOINT_VAR: "https://prod.openai.azure.com",
    "KAIRIX_NEO4J_URI": "bolt://neo4j:7687",
    "KAIRIX_NEO4J_PASSWORD": "kairix-local-dev",  # pragma: allowlist secret — fake fixture
}


class _FakeMcpServer:
    """FakeMcp server records run() calls + minimal app-builder surface.

    Satisfies what ``build_mcp_app`` needs: ``streamable_http_app()``,
    ``sse_app(mount_path=...)``, and a ``settings`` namespace. Tests rely
    on the recorder fields ``runs`` / ``streamable_calls`` / ``sse_calls``.
    """

    def __init__(self) -> None:
        self.runs: list[dict] = []
        self.streamable_calls: int = 0
        self.sse_calls: list[str] = []
        # FastMCP exposes a settings namespace that transport._apply_settings
        # writes to; emulate that as a plain SimpleNamespace.
        import types as _types

        self.settings = _types.SimpleNamespace(
            json_response=False,
            stateless_http=False,
            streamable_http_path="/mcp",
        )

    def run(self, *, transport: str) -> None:
        self.runs.append({"transport": transport})

    def streamable_http_app(self) -> Any:
        from starlette.applications import Starlette

        self.streamable_calls += 1
        return Starlette(routes=[])

    def sse_app(self, *, mount_path: str = "/sse") -> Any:
        from starlette.applications import Starlette

        self.sse_calls.append(mount_path)
        return Starlette(routes=[])


@dataclass
class _RunRecorder:
    """Captures (args, kwargs) of the uvicorn-like runner."""

    calls: list[tuple[tuple, dict]] = field(default_factory=list)

    def __call__(self, *a: Any, **kw: Any) -> None:
        self.calls.append((a, kw))


def _drive(args: list[str], deps: McpCliDeps) -> tuple[str, str, int]:
    out, err = io.StringIO(), io.StringIO()
    code = 0
    try:
        with redirect_stdout(out), redirect_stderr(err):
            mcp_main(args, deps=deps)
    except SystemExit as e:
        code = int(e.code) if e.code is not None else 0
    return out.getvalue(), err.getvalue(), code


def _build_deps(
    *,
    fake_server: _FakeMcpServer | None = None,
    fake_runner: _RunRecorder | None = None,
    warm_result: dict[str, Any] | None = None,
    operator_token: str | None = None,
) -> tuple[McpCliDeps, list[dict], _RunRecorder]:
    fake_server = fake_server or _FakeMcpServer()
    fake_runner = fake_runner or _RunRecorder()
    build_calls: list[dict] = []

    def _fake_build_server(**kwargs: Any) -> _FakeMcpServer:
        build_calls.append(kwargs)
        return fake_server

    deps = McpCliDeps(
        build_server_factory=lambda: _fake_build_server,
        uvicorn_runner_factory=lambda: fake_runner,
        warm_retrieval_stack_fn=lambda: warm_result or {"ready": True, "elapsed_ms": 1},
        # #320 — existing tests run warm SYNCHRONOUSLY so stderr /
        # readiness assertions stay deterministic. The production
        # default spawns warm in a daemon thread; the new
        # test_serve_http_binds_uvicorn_before_warm_completes test
        # pins the production thread-spawn order explicitly via its
        # own warm_runner override.
        warm_runner=lambda fn: fn(),
        # #449 — give the serve preflight a deterministic all-good env so
        # the runtime-path tests below aren't perturbed by the host's
        # ambient KAIRIX_* creds. The placeholder / fatal preflight paths
        # have their own dedicated tests that inject their own env.
        serve_env=_GOOD_SERVE_ENV,
        # #500 — pin the wizard-URL boot-print seam through the DI so the
        # default tests don't reach the real SecretsLoader (F2-clean — no
        # KAIRIX_INFRA_OPERATOR_TOKEN setenv). The wizard is always mounted
        # (the setup_wizard_web cutover flag retired, PLA-287), so the print
        # is gated only on an operator token; default None leaves the
        # pre-existing serve assertions unaffected.
        operator_token_fn=lambda: operator_token,
    )
    return deps, build_calls, fake_runner


@pytest.mark.unit
def test_serve_stdio_transport_runs_server_with_stdio(monkeypatch) -> None:
    fake_server = _FakeMcpServer()
    deps, build_calls, runner = _build_deps(fake_server=fake_server)

    _stdout, stderr, code = _drive(["serve", "--transport", "stdio"], deps)

    assert code == 0
    assert build_calls == [{"host": "127.0.0.1", "port": 8080}]
    assert fake_server.runs == [{"transport": "stdio"}]
    # uvicorn must NOT be invoked for stdio transport.
    assert runner.calls == []
    assert "Starting kairix MCP server (stdio transport)" in stderr


@pytest.mark.unit
def test_serve_http_transport_runs_uvicorn_with_resolved_app(monkeypatch) -> None:
    # Force --port so _resolve_port returns the CLI value, skipping port-scan.
    monkeypatch.setattr(sys, "argv", ["kairix", "mcp", "serve", "--port", "18099"])
    deps, build_calls, runner = _build_deps()

    _stdout, stderr, code = _drive(["serve", "--transport", "http", "--port", "18099"], deps)

    assert code == 0
    assert len(build_calls) == 1
    assert build_calls[0]["host"] == "127.0.0.1"
    assert build_calls[0]["port"] == 18099
    assert callable(build_calls[0]["readiness_check"])
    assert len(runner.calls) == 1
    pos, kwargs = runner.calls[0]
    assert pos[0] is not None  # the app object
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 18099
    assert kwargs["log_level"] == "info"
    # R1 (#387) — single worker prevents accidental vec_index duplication.
    assert kwargs["workers"] == 1
    # R1 (#387) — uvloop preferred, falls back to "auto" when not installed.
    assert kwargs["loop"] in {"uvloop", "auto"}
    # R8 (#395) — explicit long timeouts so uvicorn's 5s keep-alive default
    # cannot cut off long-running MCP tool calls (brief/prep/contradict).
    assert kwargs["timeout_keep_alive"] == 120
    assert kwargs["timeout_graceful_shutdown"] == 60
    assert "Starting kairix MCP server on http://127.0.0.1:18099/mcp" in stderr
    assert "+ /sse legacy" in stderr
    assert "warm-up complete" in stderr


@pytest.mark.unit
def test_serve_http_uvicorn_kwargs_block_short_keepalive(monkeypatch) -> None:
    """R8 (#395) regression guard: ``timeout_keep_alive`` must stay >= 60s.

    Pins the contract so a future revert (silently dropping the kwarg or
    re-introducing uvicorn's 5s default) is caught at the unit boundary
    instead of surfacing as a client-side "error" on long brief/prep calls.
    """
    monkeypatch.setattr(sys, "argv", ["kairix", "mcp", "serve", "--port", "18095"])
    deps, _build_calls, runner = _build_deps()

    _stdout, _stderr, code = _drive(["serve", "--transport", "http", "--port", "18095"], deps)

    assert code == 0
    assert len(runner.calls) == 1
    _pos, kwargs = runner.calls[0]
    # The contract: never accept a keep-alive that's anywhere near the 5s
    # default. 60s is the floor; current default is 120s.
    assert "timeout_keep_alive" in kwargs, (
        "uvicorn must be invoked with explicit timeout_keep_alive — "
        "the implicit 5s default cuts off long MCP tool calls."
    )
    assert kwargs["timeout_keep_alive"] >= 60, (
        f"timeout_keep_alive={kwargs['timeout_keep_alive']} is below the 60s "
        "floor — long MCP tool calls (brief/prep/contradict) will be cut off."
    )


@pytest.mark.unit
def test_serve_http_transport_with_no_sse_flag(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["kairix", "mcp", "serve", "--port", "18098"])
    deps, _calls, runner = _build_deps()

    _stdout, stderr, code = _drive(["serve", "--transport", "http", "--port", "18098", "--no-sse"], deps)
    assert code == 0
    assert runner.calls
    assert "(no /sse)" in stderr


@pytest.mark.unit
def test_serve_prints_tokened_wizard_url_when_token_set(monkeypatch) -> None:
    """#500 — the wizard is always mounted (the setup_wizard_web cutover
    flag retired, PLA-287), so when an operator token is configured serve
    prints the one-time tokened URL to stderr so `docker compose logs`
    surfaces the browser onboarding step."""
    monkeypatch.setattr(sys, "argv", ["kairix", "mcp", "serve", "--port", "18093"])
    # Fixture token, injected through the DI seam — not a real credential.
    token = "boot-print-fake-token"  # pragma: allowlist secret — fake fixture
    deps, _calls, _runner = _build_deps(operator_token=token)

    _stdout, stderr, code = _drive(["serve", "--transport", "http", "--port", "18093"], deps)

    assert code == 0
    assert "/setup/?operator_token=" in stderr
    assert token in stderr


@pytest.mark.unit
def test_serve_omits_tokened_url_when_no_token_configured(monkeypatch) -> None:
    """No operator token configured (loopback-only): nothing to print — the
    URL only matters when a non-loopback browser needs the grant cookie."""
    monkeypatch.setattr(sys, "argv", ["kairix", "mcp", "serve", "--port", "18091"])
    deps, _calls, _runner = _build_deps(operator_token=None)

    _stdout, stderr, code = _drive(["serve", "--transport", "http", "--port", "18091"], deps)

    assert code == 0
    assert "operator_token=" not in stderr


@pytest.mark.unit
def test_serve_http_keeps_readiness_closed_when_warmup_fails(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["kairix", "mcp", "serve", "--port", "18096"])
    deps, build_calls, runner = _build_deps(warm_result={"ready": False, "status": "error"})

    _stdout, stderr, code = _drive(["serve", "--transport", "http", "--port", "18096"], deps)

    assert code == 0
    assert runner.calls
    assert callable(build_calls[0]["readiness_check"])
    assert build_calls[0]["readiness_check"]() is False
    assert "warm-up incomplete" in stderr


@pytest.mark.unit
def test_warm_failed_event_logs_its_values_as_parseable_json(monkeypatch, caplog) -> None:
    """``event=mcp_warm_failed`` carries the warm result JSON-encoded, and a
    spaced string quoted, so log analytics can re-parse the line; never a
    Python repr (``{'ready': False}``) or a bare multi-word value."""
    import json as _json
    import logging as _logging

    monkeypatch.setattr(sys, "argv", ["kairix", "mcp", "serve", "--port", "18097"])
    warm_result = {"ready": False, "status": "error", "detail": "neo4j down"}
    deps, _build_calls, _runner = _build_deps(warm_result=warm_result)

    with caplog.at_level(_logging.INFO, logger="kairix.mcp.startup"):
        _drive(["serve", "--transport", "http", "--port", "18097"], deps)

    (line,) = [r.getMessage() for r in caplog.records if "event=mcp_warm_failed" in r.getMessage()]
    encoded = line.split("warm_result=", 1)[1]
    assert _json.loads(encoded) == warm_result
    assert '"detail": "neo4j down"' in encoded


@pytest.mark.unit
def test_serve_sse_transport_warns_and_continues_as_http(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["kairix", "mcp", "serve", "--port", "18097"])
    deps, _calls, runner = _build_deps()

    _stdout, stderr, code = _drive(["serve", "--transport", "sse", "--port", "18097"], deps)

    assert code == 0
    assert "deprecated" in stderr
    assert runner.calls, "uvicorn must still be called after the sse→http coercion"


@pytest.mark.unit
def test_serve_raises_import_error_when_build_server_unavailable() -> None:
    """When the build_server factory raises ImportError, exit 1 with operator message."""

    def _raises_import() -> Any:
        raise ImportError("mcp extra missing")

    deps = McpCliDeps(
        build_server_factory=_raises_import,
        uvicorn_runner_factory=lambda: _RunRecorder(),
        serve_env=_GOOD_SERVE_ENV,
    )

    _stdout, stderr, code = _drive(["serve"], deps)
    assert code == 1
    assert "MCP dependencies not installed" in stderr
    assert "pip install 'kairix[agents]'" in stderr


# ---------------------------------------------------------------------------
# #449 — boot-time credential preflight (warn-and-degrade vs fatal)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_serve_warns_but_starts_when_llm_key_is_placeholder(caplog) -> None:
    """A placeholder LLM key warns (naming the var) and does NOT exit — BM25 still works.

    Sabotage-proof: reverting the placeholder branch in
    ``preflight_startup_credentials`` (treat placeholder as valid) drops
    the warning so the ``KAIRIX_PROVIDER_LLM_API_KEY``/`fix:` assertions
    go RED. The value never appears in the log.
    """
    import logging as _logging

    placeholder = "your-api-key-here"
    env = {
        _LLM_API_KEY_VAR: placeholder,
        _LLM_ENDPOINT_VAR: "https://prod.openai.azure.com",
        "KAIRIX_NEO4J_URI": "bolt://neo4j:7687",
        "KAIRIX_NEO4J_PASSWORD": "kairix-local-dev",  # pragma: allowlist secret — fake fixture
    }
    deps, build_calls, _runner = _build_deps()
    deps.serve_env = env

    with caplog.at_level(_logging.WARNING, logger="kairix.mcp.startup"):
        _stdout, _stderr, code = _drive(["serve", "--transport", "stdio"], deps)

    # Did NOT exit — degrade to search-only, container boots.
    assert code == 0
    assert build_calls, "server must still be built (warn-and-degrade, not exit)"
    warning_text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert _LLM_API_KEY_VAR in warning_text
    assert any(marker in warning_text for marker in ("fix:", "next:", "run:"))
    # F15 — the placeholder/credential VALUE never appears in the warning.
    assert placeholder not in warning_text


@pytest.mark.unit
def test_serve_exits_when_neo4j_password_empty_and_uri_set() -> None:
    """Empty neo4j password + URI configured → exit 1 with the F21 affordance block.

    Sabotage-proof: reverting the neo4j-fatal branch drops the failure so
    the exit-1 assertion goes RED.
    """
    env = {
        _LLM_API_KEY_VAR: "sk-live-real-key",  # pragma: allowlist secret — fake fixture
        _LLM_ENDPOINT_VAR: "https://prod.openai.azure.com",
        "KAIRIX_NEO4J_URI": "bolt://neo4j:7687",
        "KAIRIX_NEO4J_PASSWORD": "",
    }
    deps, build_calls, _runner = _build_deps()
    deps.serve_env = env

    _stdout, stderr, code = _drive(["serve", "--transport", "http", "--port", "18094"], deps)

    assert code == 1, f"expected exit 1 on empty neo4j password; stderr={stderr!r}"
    assert "KAIRIX_NEO4J_PASSWORD" in stderr
    assert any(marker in stderr for marker in ("fix:", "next:", "run:"))
    # Server construction must NOT have proceeded past the fatal preflight.
    assert build_calls == []


@pytest.mark.unit
def test_serve_with_real_creds_does_not_warn_or_exit(caplog) -> None:
    """Real-looking creds → no warning, no exit; serve proceeds normally."""
    import logging as _logging

    deps, build_calls, _runner = _build_deps()  # _build_deps already injects _GOOD_SERVE_ENV

    with caplog.at_level(_logging.WARNING, logger="kairix.mcp.startup"):
        _stdout, _stderr, code = _drive(["serve", "--transport", "stdio"], deps)

    assert code == 0
    assert build_calls
    credential_warnings = [r for r in caplog.records if "vector" in r.getMessage().lower()]
    assert credential_warnings == [], "real creds must not produce a credential warning"


@pytest.mark.unit
def test_main_with_no_subcommand_prints_help_and_exits_1() -> None:
    """Top-level mcp invocation with no subcommand must print help + exit 1."""
    _stdout, stderr, code = _drive([], McpCliDeps())
    assert code == 1
    # argparse-formatted help mentions the serve subcommand.
    out = _stdout + stderr
    assert "serve" in out


@pytest.mark.unit
def test_resolve_port_auto_detect_when_default_available(monkeypatch) -> None:
    """No --port, no ``KAIRIX_MCP_PORT`` in ``deps.serve_env`` → ``_resolve_port``
    uses ``deps.is_port_available_fn``;
    when it returns True, port 8080 is selected.

    Drives the public ``McpCliDeps.is_port_available_fn`` /
    ``find_available_port_fn`` seams (F1-clean — no monkey-patching of
    ``kairix.platform.onboard.ports``).
    """
    monkeypatch.setattr(sys, "argv", ["kairix", "mcp", "serve"])

    deps, build_calls, _runner = _build_deps()
    deps.is_port_available_fn = lambda port: True
    deps.find_available_port_fn = lambda *, preferred: preferred
    _stdout, _stderr, code = _drive(["serve", "--transport", "http"], deps)
    assert code == 0
    assert len(build_calls) == 1
    assert build_calls[0]["host"] == "127.0.0.1"
    assert build_calls[0]["port"] == 8080
    assert callable(build_calls[0]["readiness_check"])


@pytest.mark.unit
def test_resolve_port_auto_detect_falls_back_when_default_in_use(monkeypatch) -> None:
    """No --port, no env, default port unavailable → ``find_available_port_fn`` is consulted.

    Drives the public ``McpCliDeps`` port-probe seams to pin the
    fallback-and-suggest path without touching the kairix internal
    ``kairix.platform.onboard.ports`` module.
    """
    monkeypatch.setattr(sys, "argv", ["kairix", "mcp", "serve"])

    deps, build_calls, _runner = _build_deps()
    deps.is_port_available_fn = lambda port: False
    deps.find_available_port_fn = lambda *, preferred: 19100
    _stdout, stderr, code = _drive(["serve", "--transport", "http"], deps)
    assert code == 0
    assert len(build_calls) == 1
    assert build_calls[0]["host"] == "127.0.0.1"
    assert build_calls[0]["port"] == 19100
    assert callable(build_calls[0]["readiness_check"])
    assert "Port 8080 is in use" in stderr
    assert "KAIRIX_MCP_PORT=19100" in stderr


@pytest.mark.unit
def test_resolve_port_honours_kairix_mcp_port_from_serve_env(monkeypatch) -> None:
    """No --port, ``KAIRIX_MCP_PORT`` set in ``deps.serve_env`` → that port wins
    over auto-detect. The env value reaches ``_resolve_port`` through the
    ``McpCliDeps.serve_env`` seam (F2-clean — no process-env write); the
    port-probe fakes would pick a different port if the env branch were skipped.
    """
    monkeypatch.setattr(sys, "argv", ["kairix", "mcp", "serve"])

    deps, build_calls, _runner = _build_deps()
    deps.serve_env = {**_GOOD_SERVE_ENV, "KAIRIX_MCP_PORT": "19222"}
    deps.is_port_available_fn = lambda port: True
    deps.find_available_port_fn = lambda *, preferred: 19100
    _stdout, _stderr, code = _drive(["serve", "--transport", "http"], deps)
    assert code == 0
    assert len(build_calls) == 1
    assert build_calls[0]["port"] == 19222


# ---------------------------------------------------------------------------
# #320 — port-bind order regression-lock (ADR-018 lesson: characterize first)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_serve_http_binds_uvicorn_before_warm_completes(monkeypatch) -> None:
    """#320 regression-lock: when the PRODUCTION warm_runner (daemon thread)
    is wired, uvicorn.run is invoked BEFORE warm_retrieval_stack_fn
    completes. Pre-fix this fails because warm ran synchronously.

    The test thread blocks warm via an Event until after uvicorn has
    been recorded — proving the main thread reached uvicorn.run
    without waiting on warm. Uses a local thread-spawning warm_runner
    that mirrors the production default shape (a daemon thread) so the
    test doesn't import the private _default_warm_runner helper and
    stay F5-clean.
    """
    import threading

    def _thread_spawning_warm_runner(warm_body: Callable[[], None]) -> None:
        threading.Thread(target=warm_body, daemon=True, name="kairix-mcp-warm-test").start()

    monkeypatch.setattr(sys, "argv", ["kairix", "mcp", "serve", "--port", "18095"])

    warm_may_proceed = threading.Event()
    warm_completed = threading.Event()
    call_order: list[str] = []

    def _warm_blocking() -> dict[str, Any]:
        # Will NOT return until the test releases warm_may_proceed.
        # If main() were calling warm synchronously (pre-fix), the test
        # would hang here forever because nothing releases the event
        # before main() proceeds to uvicorn — proving the bug.
        warm_may_proceed.wait(timeout=5.0)
        call_order.append("warm")
        warm_completed.set()
        return {"ready": True, "elapsed_ms": 1}

    fake_runner = _RunRecorder()

    def _runner_recording(*args: Any, **kwargs: Any) -> None:
        call_order.append("uvicorn")
        fake_runner.calls.append((args, kwargs))
        # Now release warm so it can complete and the daemon thread exits.
        warm_may_proceed.set()
        warm_completed.wait(timeout=5.0)

    deps, _build_calls, _runner = _build_deps()
    deps.warm_retrieval_stack_fn = _warm_blocking
    deps.uvicorn_runner_factory = lambda: _runner_recording
    deps.warm_runner = _thread_spawning_warm_runner  # mirrors production thread-spawning default

    _stdout, _stderr, code = _drive(["serve", "--transport", "http", "--port", "18095"], deps)

    assert code == 0
    # The fix's contract: uvicorn binds first, then warm.
    assert call_order == ["uvicorn", "warm"], (
        f"#320: uvicorn must bind BEFORE warm completes; got call_order={call_order}. "
        f"Pre-fix this assertion FAILS because warm runs synchronously before uvicorn."
    )
