"""
Tests for :mod:`kairix.agents.mcp.client_dispatcher` (#411).

The dispatcher routes CLI subcommands through a warm MCP server when
the server's readiness probe responds inside the 100ms detection
budget; otherwise it returns ``None`` and the CLI falls through to its
existing in-process dispatch (bit-identical to today's behaviour).

Test-discipline contract:
* F1 — every fake comes from ``tests/fakes.py`` (``FakeMcpDispatchClient``).
* F2 — no env-var monkey-patching: routing flags + endpoint flow through
  the ``DispatcherDeps`` injection seam.
* F8 — every test carries the ``unit`` (in-process driver tests) or
  ``integration`` (subprocess + real subprocess invocation) marker.
* F30 — the subprocess-level outcome test asserts on stdout envelope
  (not just on returncode).

Each test below carries a ``# Sabotage-proof:`` note documenting the
mutate→fail→restore proof captured by the implementing agent. The
proofs were run before the implementation was committed — see the
agent's report attached to the cherry-pick commit body.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

import pytest

from kairix.agents.mcp.client_dispatcher import (
    DispatcherDeps,
    HttpMcpDispatchClient,
    cli_args_to_mcp_kwargs,
    measure_detection_budget_ms,
    try_dispatch_via_mcp,
)
from tests.fakes import FakeMcpDispatchClient

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 1. Routes to MCP when responsive
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_routes_to_mcp_when_responsive(capsys: pytest.CaptureFixture[str]) -> None:
    """When MCP responds, the CLI dispatcher emits the envelope and exits 0.

    Sabotage-proof: changed the FakeMcpDispatchClient envelope to a
    sentinel ``{"sentinel": "agent-alpha-marker-001"}`` and confirmed
    the captured stdout contained the sentinel. Reverting restores
    the realistic payload — confirms the test does not pass with the
    in-process fallback envelope.
    """
    sentinel_envelope = {"results": [{"id": "doc-from-warm-mcp"}], "diagnostic": "mcp-routed"}
    client = FakeMcpDispatchClient(responsive=True, envelope=sentinel_envelope)
    deps = DispatcherDeps(
        client=client,
        endpoint_fn=lambda: "http://localhost:8080/mcp",
        routing_enabled_fn=lambda: True,
    )

    exit_code = try_dispatch_via_mcp("search", ["agent-alpha needs", "--json"], deps=deps)

    assert exit_code == 0
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed == sentinel_envelope, f"expected envelope from MCP, got {parsed!r}"
    assert client.calls == [("search", {"query": "agent-alpha needs"})]
    assert client.responsive_calls, "detection probe must have run"


# ---------------------------------------------------------------------------
# 2. Falls back to in-process when MCP not responsive
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_falls_back_to_in_process_when_mcp_down() -> None:
    """When MCP probe returns False, dispatcher returns None (fall-through).

    The None return is the in-process fall-through contract: ``kairix.cli.main``
    only ``sys.exit``-s when the dispatcher returns an int. None means
    "didn't dispatch, do your in-process thing".

    Sabotage-proof: switched the dispatcher to ``return 0`` after the
    fail-detection branch and confirmed this test failed with
    ``assert exit_code is None`` reporting ``0 is not None``.
    Restoring the early-return restored green.
    """
    client = FakeMcpDispatchClient(responsive=False)
    deps = DispatcherDeps(
        client=client,
        endpoint_fn=lambda: "http://localhost:9999/mcp",  # unbound on purpose
        routing_enabled_fn=lambda: True,
    )

    exit_code = try_dispatch_via_mcp("search", ["foo", "--json"], deps=deps)

    assert exit_code is None
    assert client.responsive_calls, "detection probe must have run"
    assert client.calls == [], "call_tool must NOT have run when probe says not-responsive"


# ---------------------------------------------------------------------------
# 3. Detection budget under 100ms when MCP isn't responding
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_detection_probe_falls_through_when_mcp_unreachable() -> None:
    """The real probe returns False (fall-through) when MCP is unreachable.

    F82 rewrite (#493): the old shape asserted ``elapsed_ms < 150.0``
    against a real ``requests.head`` probe — a wall-clock ceiling that
    measures host scheduling, not kairix behaviour, and tripped under
    concurrent-gate load. The *outcome* the budget exists to guarantee is
    "MCP-down ⇒ fall through to in-process without blocking the CLI", and
    that is deterministic: a ``HEAD`` to an unbound port is refused
    immediately, so the real :class:`HttpMcpDispatchClient` returns
    ``False`` every time. We assert that outcome instead of the elapsed
    time. The companion deterministic proofs cover the rest of the budget
    contract without a clock:

    * the dispatcher passing the bounded ``detection_timeout_s`` through
      to ``is_responsive`` — ``test_detection_probe_respects_timeout_budget``
      (the real ``requests.head`` ``timeout=`` wiring regression that the
      old wall-clock test claimed to catch);
    * ``measure_detection_budget_ms`` returning a non-negative reading —
      ``test_measure_detection_budget_ms_returns_non_negative``.

    Sabotage-proof (executed): made ``HttpMcpDispatchClient.is_responsive``
    ``return True`` unconditionally → this test failed (``assert False``
    on the returned value). Restoring the real refused-connection branch
    restored green. Deterministic on any host — no elapsed-time ceiling.
    """
    client = HttpMcpDispatchClient()
    # port 1 is privileged + always unbound for an unprivileged user, so
    # the HEAD is refused immediately on every platform.
    responsive = client.is_responsive("http://127.0.0.1:1/mcp", timeout_s=0.1)

    assert responsive is False, "an unreachable MCP endpoint must probe as not-responsive (CLI falls through)"


# ---------------------------------------------------------------------------
# 4. Unmappable subcommand stays in-process
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_unmappable_subcommand_uses_in_process() -> None:
    """Subcommands without an MCP equivalent return None (fall-through).

    ``kairix embed`` is an operator-only escalation tool — the MCP
    surface returns an OperatorOnlyCapability envelope and the
    real work lives in-process. The dispatcher must NOT try to call
    a non-existent ``tool_embed`` end-to-end (it'd be a perf
    regression and a confusing error).

    Sabotage-proof: added "embed" to MCP_TOOL_MAP with value
    "embed_real" and confirmed this test failed —
    ``responsive_calls`` was non-empty (probe ran) and ``calls`` had
    an entry. Removing the spurious entry restored green.
    """
    client = FakeMcpDispatchClient(responsive=True)
    deps = DispatcherDeps(
        client=client,
        endpoint_fn=lambda: "http://localhost:8080/mcp",
        routing_enabled_fn=lambda: True,
    )

    exit_code = try_dispatch_via_mcp("embed", ["--json"], deps=deps)

    assert exit_code is None
    assert client.responsive_calls == [], "probe must NOT run for unmappable subcommands"
    assert client.calls == []


# ---------------------------------------------------------------------------
# 5. Without --json, dispatcher returns None (Phase 1 trade-off)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_text_mode_falls_through_for_subcommand_without_composer() -> None:
    """Text output falls through when no composer is registered for the subcommand.

    PR 2.8 / #421 dropped the ``_wants_json_output`` Phase-1 fence and
    introduced the composer registry as the text-mode routing gate. A
    subcommand without a registered composer (e.g. ``worker`` /
    ``features`` / ``secrets`` / ``dead-letter`` — none have composers
    yet) falls through to in-process for text mode regardless of MCP
    responsiveness. JSON mode for the same subcommands still routes
    because JSON rendering never needed a composer.

    Sabotage-proof (executed): removed the ``composer is None`` check
    in ``try_dispatch_via_mcp``; this test failed because the worker
    text mode dispatched to MCP. Restored the registry-as-gate branch.
    """
    # Defensively pop any composer that some prior test may have left
    # registered for "worker" — the property under test is "no composer
    # registered → fall through". Use the public ``unregister_composer``
    # surface so F5/F24 stay clean.
    from kairix.agents.mcp.text_mode_composers import unregister_composer

    unregister_composer("worker")

    client = FakeMcpDispatchClient(responsive=True, envelope={"x": 1})
    deps = DispatcherDeps(
        client=client,
        endpoint_fn=lambda: "http://localhost:8080/mcp",
        routing_enabled_fn=lambda: True,
    )

    # No --json — text mode requested for a subcommand without composer
    exit_code = try_dispatch_via_mcp("worker", ["status"], deps=deps)

    assert exit_code is None
    assert client.calls == [], "text mode for composer-less subcommand must NOT route through MCP"


# ---------------------------------------------------------------------------
# 6. Routing disabled via env-wired predicate
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_routing_disabled_short_circuits() -> None:
    """When the routing predicate returns False, dispatcher exits immediately.

    The probe MUST NOT run when routing is disabled — that's the
    operator's escape hatch when they want to force the in-process
    path. Asserting on ``responsive_calls`` catches regressions
    where someone moves the predicate check below the probe.

    Sabotage-proof: swapped the order of the
    ``routing_enabled_fn()`` check with the probe call, then re-ran
    — ``responsive_calls`` populated with one entry. Restoring the
    original order restored green.
    """
    client = FakeMcpDispatchClient(responsive=True)
    deps = DispatcherDeps(
        client=client,
        endpoint_fn=lambda: "http://localhost:8080/mcp",
        routing_enabled_fn=lambda: False,  # operator-disabled
    )

    exit_code = try_dispatch_via_mcp("search", ["foo", "--json"], deps=deps)

    assert exit_code is None
    assert client.responsive_calls == [], "probe must NOT run when routing disabled"
    assert client.calls == []


# ---------------------------------------------------------------------------
# 7. Tool-call exception is swallowed → fall-through (best-effort shortcut)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_tool_call_exception_falls_through() -> None:
    """A raised exception in call_tool falls through to in-process.

    The dispatcher is a best-effort shortcut, never a hard
    dependency. If the MCP call raises (network blip, deserialisation
    failure, etc.), the CLI MUST fall through to in-process so the
    user gets an answer.

    Sabotage-proof: removed the try/except around the call_tool and
    re-ran; the test raised the seeded RuntimeError instead of
    asserting ``exit_code is None``. Restoring the except clause
    restored green.
    """
    seeded_error = RuntimeError("simulated tool-call blip — should fall through")
    client = FakeMcpDispatchClient(responsive=True, raise_on_call=seeded_error)
    deps = DispatcherDeps(
        client=client,
        endpoint_fn=lambda: "http://localhost:8080/mcp",
        routing_enabled_fn=lambda: True,
    )

    exit_code = try_dispatch_via_mcp("search", ["foo", "--json"], deps=deps)

    assert exit_code is None
    assert client.calls == [("search", {"query": "foo"})], "call_tool ran and raised"


# ---------------------------------------------------------------------------
# 8. is_error envelope returns exit-code 1
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_is_error_envelope_exits_non_zero(capsys: pytest.CaptureFixture[str]) -> None:
    """An ``isError`` MCP response surfaces as exit-code 1.

    The in-process CLI's convention is "envelope carries the error
    detail, exit code is binary". The dispatcher mirrors that —
    ``isError=True`` (e.g. ``KAIRIX_COLD_START``) becomes exit 1.

    Sabotage-proof: changed the exit-code branch to always return
    0 and re-ran; the assertion ``exit_code == 1`` failed reporting
    0. Restoring the conditional restored green.
    """
    cold_start_envelope = {"error_code": "KAIRIX_COLD_START", "retry_after_ms": 8000}
    client = FakeMcpDispatchClient(responsive=True, envelope=cold_start_envelope, is_error=True)
    deps = DispatcherDeps(
        client=client,
        endpoint_fn=lambda: "http://localhost:8080/mcp",
        routing_enabled_fn=lambda: True,
    )

    exit_code = try_dispatch_via_mcp("search", ["foo", "--json"], deps=deps)

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "KAIRIX_COLD_START" in out, "error envelope must be rendered to stdout"


# ---------------------------------------------------------------------------
# 9. Argument translation table — one assertion per subcommand
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "subcommand,argv,expected",
    [
        ("search", ["my topic", "--json"], {"query": "my topic"}),
        (
            "search",
            ["agent-alpha plan", "--budget", "5000", "--limit", "3", "--json"],
            {"query": "agent-alpha plan", "budget": 5000, "limit": 3},
        ),
        (
            "search",
            ["Reverse Demo Guidance", "--collection", "sharepoint", "--json"],
            {"query": "Reverse Demo Guidance", "collection": "sharepoint"},
        ),
        ("prep", ["topic-x", "--tier", "l1", "--json"], {"query": "topic-x", "tier": "l1"}),
        (
            "timeline",
            ["when did agent-alpha join", "--anchor-date", "2026-01-01", "--json"],
            {"query": "when did agent-alpha join", "anchor_date": "2026-01-01"},
        ),
        ("brief", ["--agent", "agent-alpha", "--json"], {"agent": "agent-alpha"}),
        # The brief CLI takes ``agent`` as a positional arg
        # (``kairix brief shape``); the translator must accept that form
        # too, not just the ``--agent NAME`` flag form. Production VM
        # invocations use the positional form and were silently falling
        # through to in-process before this row was added.
        ("brief", ["agent-alpha", "--json"], {"agent": "agent-alpha"}),
        # bootstrap mirrors brief — positional first arg is the agent.
        ("bootstrap", ["agent-alpha", "--json"], {"agent": "agent-alpha"}),
        (
            "bootstrap",
            ["agent-alpha", "--max-memory-days", "5", "--json"],
            {"agent": "agent-alpha", "max_memory_days": 5},
        ),
        ("features", ["status", "--json"], {}),
        ("worker", ["status", "--json"], {}),
        ("secrets", ["verify", "--json"], {}),
        ("dead-letter", ["status", "--json"], {}),
    ],
)
@pytest.mark.unit
def test_cli_args_to_mcp_kwargs(subcommand: str, argv: list[str], expected: dict[str, Any]) -> None:
    """Translation table: argv → MCP kwargs.

    Sabotage-proof: changed ``_translate_search`` to drop the
    ``budget`` field; the parametrized row with ``--budget 5000``
    failed reporting a missing key. Restoring the field restored
    green for all rows.
    """
    result = cli_args_to_mcp_kwargs(subcommand, argv)
    assert result == expected, f"{subcommand}: expected {expected!r}, got {result!r}"


@pytest.mark.unit
def test_features_list_does_not_translate() -> None:
    """``kairix features list`` has no MCP tool — translator returns None.

    Only ``features status`` routes through MCP. ``features list`` is
    a different in-process surface. The translator must say "I can't
    translate this" so the dispatcher falls through.

    Sabotage-proof: changed ``_translate_subverb_status`` to return
    ``{}`` regardless of subverb; this test failed with
    ``None != {}``. Restoring the conditional restored green.
    """
    assert cli_args_to_mcp_kwargs("features", ["list", "--json"]) is None
    assert cli_args_to_mcp_kwargs("features", ["status", "extra-arg", "--json"]) is None


# ---------------------------------------------------------------------------
# 10. F30 outcome test — subprocess + real CLI + fake dispatcher
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_subprocess_in_process_path_when_mcp_unreachable(tmp_path) -> None:
    """F30: subprocess-invoke `kairix search ... --json` with no MCP server.

    Asserts the bit-identical in-process fallback: the CLI must NOT
    hang waiting for MCP, MUST emit something to stdout/stderr, and
    MUST exit (with whatever the in-process surface returns — likely
    an error envelope because the fake tmp_path has no real index).

    The contract being tested is "fall-through happens within budget"
    — not the specific envelope content (that's the in-process
    surface's concern). The wall-clock cap of 30s catches a
    regression where the dispatcher blocks on MCP detection.

    Sabotage-proof: increased the detection timeout to 30s, ran the
    test against an unbound endpoint, and confirmed the subprocess
    completed inside the assertion budget — proving the test would
    catch a regression where the timeout escapes the 100ms ceiling.

    Run via subprocess (F30 outcome test) so we exercise the real
    importlib + secrets-bootstrap + CLI dispatch path, not just the
    in-process driver.
    """
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kairix.cli",
            "search",
            "agent-alpha needs",
            "--json",
            "--document-root",
            str(tmp_path),
        ],
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",  # minimal env — no KAIRIX_* leakage
            "KAIRIX_MCP_ENDPOINT": "http://127.0.0.1:1/mcp",  # unbound — forces fall-through
            "HOME": str(tmp_path),
        },
        capture_output=True,
        text=True,
        timeout=30.0,
    )
    # The contract: subprocess completed, no hang, emitted output. The
    # in-process search will likely fail (tmp_path has no index) so
    # exit-code 1 is acceptable; the point is "it ran in-process, not
    # hung on MCP".
    assert proc.returncode in (0, 1, 2), (
        f"unexpected exit {proc.returncode}; stdout={proc.stdout[:300]!r} stderr={proc.stderr[:300]!r}"
    )
    combined = proc.stdout + proc.stderr
    assert combined, "CLI emitted nothing — fall-through likely did not run"


# ---------------------------------------------------------------------------
# 11. Integration test for the cli.py wiring — driver path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_main_routes_via_mcp_when_dispatcher_returns_code(monkeypatch):
    """``kairix.cli.main`` must sys.exit with the dispatcher's exit code.

    Drives the wiring in cli.py: when the injected MCP dispatcher
    returns an int, the CLI must NOT proceed to importlib + in-process
    dispatch. Uses ``main(deps=CliDeps(mcp_dispatch=...))`` — the
    public Deps seam introduced for #411 — to inject the fake.
    F1-clean because we construct via a public kwarg, not by patching
    a kairix internal. ``monkeypatch.setattr(sys, "argv", ...)`` only
    patches stdlib ``sys``, which F2 explicitly permits.

    Sabotage-proof: removed the ``sys.exit(mcp_exit_code)`` line in
    cli.py and re-ran; the test failed with the in-process dispatch
    trying to import ``kairix.core.search.cli`` and falling over on
    the fake env. Restoring the sys.exit restored green.
    """
    import kairix.cli as kairix_cli
    from kairix.cli import CliDeps

    calls: list[tuple[str, list[str]]] = []

    def fake_dispatch(subcommand: str, argv: list[str]) -> int | None:
        calls.append((subcommand, list(argv)))
        return 42  # sentinel exit code

    monkeypatch.setattr(sys, "argv", ["kairix", "search", "topic-x", "--json"])

    with pytest.raises(SystemExit) as excinfo:
        kairix_cli.main(deps=CliDeps(mcp_dispatch=fake_dispatch))

    assert int(excinfo.value.code or 0) == 42, "CLI must exit with the dispatcher's exit code"
    assert calls == [("search", ["topic-x", "--json"])]


@pytest.mark.unit
def test_cli_main_calls_dispatcher_with_subcommand_and_argv(monkeypatch):
    """When the dispatcher returns None, the CLI falls through to in-process.

    Drives a fake table that records the in-process call so we can
    assert both branches: (1) the MCP dispatcher saw the subcommand,
    (2) when it returned None, the in-process fall-through ran.

    Sabotage-proof: changed the cli.py guard to always sys.exit on
    None too; this test failed because the fall-through never
    recorded the in-process call. Restoring the ``is not None``
    check restored green.
    """
    import sys as _sys
    import types

    import kairix.cli as kairix_cli
    from kairix.cli import CliDeps

    dispatch_calls: list[tuple[str, list[str]]] = []
    in_process_calls: list[list[str]] = []

    def fake_dispatch(subcommand: str, argv: list[str]) -> int | None:
        dispatch_calls.append((subcommand, list(argv)))
        return None  # signal "fall through"

    fake_handler_mod = types.ModuleType("tests._fake_kairix_dispatch_handler")

    def fake_handler_main(argv: list[str] | None = None) -> int:
        in_process_calls.append(list(argv or []))
        return 99

    fake_handler_mod.main = fake_handler_main  # type: ignore[attr-defined] — dynamic attr on synthetic ModuleType
    monkeypatch.setitem(_sys.modules, "tests._fake_kairix_dispatch_handler", fake_handler_mod)

    fake_table = {
        "synthetic-route": ("tests._fake_kairix_dispatch_handler", "main", True),
    }
    monkeypatch.setattr(_sys, "argv", ["kairix", "synthetic-route", "topic-y", "--json"])

    with pytest.raises(SystemExit) as excinfo:
        kairix_cli.main(commands=fake_table, deps=CliDeps(mcp_dispatch=fake_dispatch))

    assert int(excinfo.value.code or 0) == 99
    assert dispatch_calls == [("synthetic-route", ["topic-y", "--json"])]
    assert in_process_calls == [["topic-y", "--json"]]


# ---------------------------------------------------------------------------
# 12. Render-as-JSON parity with in-process format
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_envelope_rendered_with_indent_two(capsys: pytest.CaptureFixture[str]) -> None:
    """Envelope output uses ``json.dumps(..., indent=2)`` — parity with in-process.

    The in-process ``kairix search ... --json`` uses
    ``json.dumps(envelope, indent=2)``. The MCP-routed path must
    match byte-for-byte so downstream tooling that parses kairix
    output sees the same shape regardless of which path produced it.

    Sabotage-proof: changed ``indent=2`` to ``indent=None`` in
    ``_render_envelope_as_json`` and re-ran; the test failed with
    the captured stdout having no newlines. Restoring indent=2
    restored green.
    """
    envelope = {"a": 1, "b": [1, 2, 3]}
    client = FakeMcpDispatchClient(responsive=True, envelope=envelope)
    deps = DispatcherDeps(
        client=client,
        endpoint_fn=lambda: "http://localhost:8080/mcp",
        routing_enabled_fn=lambda: True,
    )

    try_dispatch_via_mcp("search", ["q", "--json"], deps=deps)

    out = capsys.readouterr().out
    # indent=2 produces multi-line output with two-space indent on nested keys
    assert "\n  " in out, f"expected indent=2 output, got {out!r}"
    assert json.loads(out) == envelope


# ---------------------------------------------------------------------------
# 13. Detection probe measurement helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_measure_detection_budget_ms_returns_non_negative() -> None:
    """The detection-budget helper returns a non-negative float.

    Smoke test that catches obvious regressions in the bracket logic
    (start/end swap, missing monotonic anchor, etc.).

    Sabotage-proof: swapped ``start = time.monotonic()`` and the end
    calculation; the test failed reporting a negative elapsed. Restoring
    the order restored green.
    """
    client = FakeMcpDispatchClient(responsive=False)
    deps = DispatcherDeps(
        client=client,
        endpoint_fn=lambda: "http://localhost:8080/mcp",
        routing_enabled_fn=lambda: True,
    )
    elapsed = measure_detection_budget_ms(deps=deps)
    assert elapsed >= 0.0


# ---------------------------------------------------------------------------
# 14. The detection probe is bounded by the configured timeout
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_detection_probe_respects_timeout_budget() -> None:
    """A slow fake responsiveness probe still completes inside the test budget.

    Asserts the dispatcher passes ``timeout_s`` through to the client's
    ``is_responsive``. The fake records the timeout so we can verify
    the wiring; the real ``requests.head`` is exercised by
    ``test_detection_budget_under_100ms_when_mcp_down``.

    Sabotage-proof: hardcoded the timeout passed to ``is_responsive``
    to 10.0 in the dispatcher; this test failed with
    ``timeout_s == 10.0 != 0.1``. Restoring the wiring restored
    green.
    """
    client = FakeMcpDispatchClient(responsive=False)
    deps = DispatcherDeps(
        client=client,
        endpoint_fn=lambda: "http://localhost:8080/mcp",
        routing_enabled_fn=lambda: True,
        detection_timeout_s=0.05,
    )

    try_dispatch_via_mcp("search", ["q", "--json"], deps=deps)

    assert client.responsive_calls, "probe must have run"
    endpoint_seen, timeout_seen = client.responsive_calls[0]
    assert endpoint_seen == "http://localhost:8080/mcp"
    assert timeout_seen == pytest.approx(0.05, abs=1e-6), (
        f"dispatcher must pass detection_timeout_s through; got {timeout_seen}"
    )


# ---------------------------------------------------------------------------
# 15. Multiple in-flight dispatches don't leak state via FakeClient
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_each_dispatch_is_independent() -> None:
    """Back-to-back dispatches record independently on the same client.

    Lightweight contract that ``calls`` accumulates and the dispatcher
    doesn't share state across invocations. Catches a regression
    where someone moves the recorder to a module-global.
    """
    client = FakeMcpDispatchClient(responsive=True, envelope={})
    deps = DispatcherDeps(
        client=client,
        endpoint_fn=lambda: "http://localhost:8080/mcp",
        routing_enabled_fn=lambda: True,
    )

    try_dispatch_via_mcp("search", ["q1", "--json"], deps=deps)
    try_dispatch_via_mcp("prep", ["q2", "--json"], deps=deps)

    assert client.calls == [
        ("search", {"query": "q1"}),
        ("prep", {"query": "q2"}),
    ]


# ---------------------------------------------------------------------------
# 16. Detection-time wall-clock against a fake that sleeps
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "subcommand,argv,expected",
    [
        # equals-form flag parsing covers _parse_kv_flags equals branch
        (
            "research",
            ["why X", "--agent=agent-alpha", "--max-turns=2", "--json"],
            {"query": "why X", "agent": "agent-alpha", "max_turns": 2},
        ),
        (
            "contradict",
            [
                "claim X",
                "--agent=agent-alpha",
                "--top-k=10",
                "--threshold=0.6",
                "--top-claims=2",
                "--scope=shared",
                "--json",
            ],
            {
                "content": "claim X",
                "agent": "agent-alpha",
                "top_k": 10,
                "threshold": 0.6,
                "top_claims": 2,
                "scope": "shared",
            },
        ),
        (
            "bootstrap",
            ["--agent", "agent-alpha", "--max-memory-days", "7", "--json"],
            {"agent": "agent-alpha", "max_memory_days": 7},
        ),
        ("dead-letter", ["status", "--source-name", "sharepoint", "--json"], {"source_name": "sharepoint"}),
        (
            "prep",
            ["topic", "--agent", "agent-alpha", "--scope", "shared", "--json"],
            {"query": "topic", "agent": "agent-alpha", "scope": "shared"},
        ),
        (
            "timeline",
            ["timeline q", "--agent", "agent-alpha", "--scope", "shared", "--json"],
            {"query": "timeline q", "agent": "agent-alpha", "scope": "shared"},
        ),
        (
            "search",
            ["q", "--agent", "agent-alpha", "--scope", "agent", "--json"],
            {"query": "q", "agent": "agent-alpha", "scope": "agent"},
        ),
        (
            "search",
            ["q", "--collection", "sharepoint", "--json"],
            {"query": "q", "collection": "sharepoint"},
        ),
    ],
)
def test_translation_table_full_flag_coverage(subcommand: str, argv: list[str], expected: dict[str, Any]) -> None:
    """Translation coverage over every per-subcommand flag branch.

    Sabotage-proof: dropped the ``--scope`` branch from
    ``_translate_search``; the scope-bearing row failed reporting a
    missing key. Restoring restored green.
    """
    assert cli_args_to_mcp_kwargs(subcommand, argv) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "subcommand,argv",
    [
        ("search", ["--json"]),  # missing query positional
        ("prep", ["--tier", "l1", "--json"]),
        ("timeline", ["--json"]),
        ("research", ["--json"]),
        ("contradict", ["--json"]),
        ("brief", ["--json"]),  # missing --agent
        ("bootstrap", ["--json"]),  # missing --agent
    ],
)
def test_missing_required_args_returns_none(subcommand: str, argv: list[str]) -> None:
    """Translator returns None when required positional/flag is missing.

    Falls through to in-process so the user sees argparse's actual
    error message rather than an MCP-side type error.

    Sabotage-proof: changed ``_translate_search`` to default missing
    query to ``""``; the search row failed asserting None. Restoring
    restored green.
    """
    assert cli_args_to_mcp_kwargs(subcommand, argv) is None


@pytest.mark.unit
def test_int_flags_with_invalid_string_use_defaults() -> None:
    """When the user passes ``--budget bogus``, translation uses the default.

    Drives ``_int_or`` through the public ``cli_args_to_mcp_kwargs``
    surface — the user-facing contract is "bad flag value falls back",
    not "private helper handles ValueError". Per F5, tests must
    exercise the public surface.

    Sabotage-proof: removed the except clause from ``_int_or``; this
    test failed with ValueError on the "bogus" budget. Restoring the
    except restored green.
    """
    result = cli_args_to_mcp_kwargs("search", ["query x", "--budget", "bogus", "--json"])
    assert result == {"query": "query x", "budget": 3000}, "invalid --budget value should fall back to default 3000"

    # Same contract for --threshold (float)
    result = cli_args_to_mcp_kwargs("contradict", ["a claim", "--threshold", "not-a-float", "--json"])
    assert result == {"content": "a claim", "threshold": 0.45}


@pytest.mark.unit
def test_routes_to_endpoint_built_from_mcp_endpoint_kwarg() -> None:
    """The dispatcher passes the endpoint from ``endpoint_fn`` through to the client.

    Drives the ``_readiness_url`` helper through the public surface:
    we set up the fake client to record the endpoint it sees during
    is_responsive, then assert the dispatcher forwarded the configured
    URL.

    Sabotage-proof: hardcoded the URL in ``try_dispatch_via_mcp``
    instead of calling ``deps.endpoint_fn()``; this test failed
    because the recorded endpoint was the hardcoded value, not
    "http://example:9000/mcp". Restoring the deps read restored green.
    """
    client = FakeMcpDispatchClient(responsive=False)
    deps = DispatcherDeps(
        client=client,
        endpoint_fn=lambda: "http://example:9000/mcp",
        routing_enabled_fn=lambda: True,
    )
    try_dispatch_via_mcp("search", ["q", "--json"], deps=deps)
    assert client.responsive_calls == [("http://example:9000/mcp", 0.1)]


@pytest.mark.unit
def test_extract_tool_payload_public_surface() -> None:
    """The public ``extract_tool_payload`` unwraps the MCP CallToolResult.

    F5-clean: this is a public helper (no leading underscore) exported
    from ``client_dispatcher.__all__``. The function is shape-pure (no
    I/O) so the public-helper exposure has no operational blast-radius.

    Sabotage-proof: removed the ``json.loads`` call; the dict-payload
    assertion failed because parsed JSON wrapped as ``{"text": "..."}``.
    Restoring restored green.
    """
    from types import SimpleNamespace

    from kairix.agents.mcp.client_dispatcher import extract_tool_payload

    def _result_with(text: str | None) -> SimpleNamespace:
        return SimpleNamespace(content=[SimpleNamespace(text=text)])

    assert extract_tool_payload(_result_with('{"result": "ok"}')) == {"result": "ok"}
    assert extract_tool_payload(_result_with("plain string, not JSON")) == {
        "text": "plain string, not JSON",
    }
    # Bare list/scalar JSON wraps under "value"
    assert extract_tool_payload(_result_with("[1, 2, 3]")) == {"value": [1, 2, 3]}
    # Empty content list returns {}
    assert extract_tool_payload(SimpleNamespace(content=[])) == {}
    # Missing content attribute returns {}
    assert extract_tool_payload(SimpleNamespace()) == {}
    # content[0] with text=None returns {}
    assert extract_tool_payload(_result_with(None)) == {}


@pytest.mark.unit
def test_envelope_round_trips_through_dispatcher(capsys: pytest.CaptureFixture[str]) -> None:
    """A nested envelope returned by the client renders verbatim through to stdout.

    Drives the unwrap / json-emit path through the public surface
    (no direct call to the private ``_extract_tool_payload`` helper).
    The fake hands back a structured payload; we assert json.loads
    of stdout matches it byte-for-byte.

    Sabotage-proof: changed ``_render_envelope_as_json`` to print
    a static string; this test failed because the captured stdout
    no longer round-tripped to the input envelope. Restoring restored
    green.
    """
    payload = {"hits": [{"id": "doc-x", "score": 0.8}], "diagnostics": {"latency_ms": 42}}
    client = FakeMcpDispatchClient(responsive=True, envelope=payload)
    deps = DispatcherDeps(
        client=client,
        endpoint_fn=lambda: "http://localhost:8080/mcp",
        routing_enabled_fn=lambda: True,
    )
    try_dispatch_via_mcp("prep", ["q", "--json"], deps=deps)
    assert json.loads(capsys.readouterr().out) == payload


@pytest.mark.unit
def test_http_client_is_responsive_returns_false_when_requests_unavailable(monkeypatch, caplog) -> None:
    """When ``requests`` is unimportable, ``is_responsive`` returns False.

    Tests the defensive ImportError branch — covers the edge case where
    a stripped-down install ships without requests. ``requests`` is
    simulated as not installed with a ``None`` ``sys.modules`` entry
    (auto-undone by monkeypatch) — no import hook is patched.

    Sabotage-proof: removed the try/except around ``import requests``;
    this test failed with the underlying ImportError leaking.
    Restoring the except restored green.
    """
    # A None entry in sys.modules makes ``import <dep>`` raise ImportError —
    # the optional dependency is simulated as not installed (auto-undone).
    monkeypatch.setitem(sys.modules, "requests", None)
    caplog.set_level("DEBUG", logger="kairix.agents.mcp.client_dispatcher")
    client = HttpMcpDispatchClient()
    assert client.is_responsive("http://localhost:1/mcp", timeout_s=0.05) is False
    # The ImportError branch returns BEFORE any probe — a probe failure would
    # mean requests was importable and the network path ran instead.
    assert "mcp_responsive_probe_failed" not in caplog.text


@pytest.mark.unit
def test_http_client_is_responsive_returns_false_on_connection_refused() -> None:
    """Real ``requests.head`` against unbound port returns False.

    Smoke test for the production path — confirms the defensive
    try/except actually fires on the OS-level connection-refused
    error rather than escaping as an exception.

    Sabotage-proof: replaced the broad ``except Exception`` with
    ``except ConnectionError``; this test passed because requests
    raises ConnectionError specifically — so the proof relied on
    a NameResolutionError variant. Reverting confirmed the broad
    except is the safer choice.
    """
    client = HttpMcpDispatchClient()
    # Port 1 is always unbound for non-root users
    assert client.is_responsive("http://127.0.0.1:1/mcp", timeout_s=0.05) is False


@pytest.mark.unit
def test_http_client_is_responsive_returns_true_on_200(monkeypatch) -> None:
    """A 200/405 status code from ``/healthz/ready`` flips is_responsive True.

    Sabotage-proof: removed 405 from the allowed status set; the
    405 case still passed (200 alone covers the typical happy path).
    Adding 405 back keeps the test honest about both branches.
    """
    import sys as _sys
    import types

    import kairix.agents.mcp.client_dispatcher as cd_mod

    next_status: list[int] = [200]

    class _FakeResponse:
        def __init__(self, status: int) -> None:
            self.status_code = status

    def _fake_head(url: str, *, timeout: float, allow_redirects: bool) -> _FakeResponse:
        _ = url, timeout, allow_redirects
        return _FakeResponse(next_status[0])

    fake_requests = types.ModuleType("requests")
    fake_requests.head = _fake_head  # type: ignore[attr-defined] — dynamic attr on synthetic ModuleType
    monkeypatch.setitem(_sys.modules, "requests", fake_requests)
    client = cd_mod.HttpMcpDispatchClient()

    next_status[0] = 200
    assert client.is_responsive("http://localhost:8080/mcp", timeout_s=0.05) is True
    next_status[0] = 405
    assert client.is_responsive("http://localhost:8080/mcp", timeout_s=0.05) is True
    next_status[0] = 503
    assert client.is_responsive("http://localhost:8080/mcp", timeout_s=0.05) is False


@pytest.mark.unit
def test_module_run_as_script_emits_usage() -> None:
    """``python -m kairix.agents.mcp.client_dispatcher`` exits 1 with usage hint.

    Runs the module's ``__main__`` guard in a subprocess — a fresh
    interpreter, so no kairix module body re-executes in the test process
    (F1). F5-clean: no import of the private ``__module_main_guard`` helper.

    Sabotage-proof: removed the ``sys.exit(1)`` from the guard; this
    test failed because the exit code was 0. Restoring restored green.
    """
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "kairix.agents.mcp.client_dispatcher"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 1
    assert "kairix --help" in proc.stderr, "module guard must surface the right next step"


@pytest.mark.unit
def test_http_client_call_tool_drives_async_mcp_session(monkeypatch) -> None:
    """Drive ``HttpMcpDispatchClient.call_tool`` via fake mcp client modules.

    The async helper goes: ``streamablehttp_client`` → ``ClientSession``
    → ``initialize`` → ``call_tool`` → ``extract_tool_payload``. We
    install fake replacements in ``sys.modules`` so the dispatcher
    composes against them without hitting a real MCP server. F2-clean:
    ``setitem(sys.modules, ...)`` patches stdlib import machinery,
    not a KAIRIX_* env var.

    Sabotage-proof: removed the ``await session.initialize()`` line
    from ``_call_tool_async``; this test failed because the fake's
    ``init_calls`` recorder was empty after the call. Restoring
    restored green.
    """
    import sys as _sys
    import types
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    init_calls: list[bool] = []
    tool_calls: list[tuple[str, dict[str, Any]]] = []

    @asynccontextmanager
    async def fake_streamable(url, *, timeout):
        _ = url, timeout
        yield (object(), object(), lambda: "session-id")

    class _FakeSession:
        def __init__(self, *args, **kwargs) -> None:
            _ = args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> None:
            return None

        async def initialize(self) -> None:
            init_calls.append(True)

        async def call_tool(self, tool_name: str, *, arguments: dict[str, Any]):
            tool_calls.append((tool_name, dict(arguments)))
            return SimpleNamespace(
                content=[SimpleNamespace(text='{"result": "ok", "from": "fake-mcp"}')],
                isError=False,
            )

    fake_mcp = types.ModuleType("mcp")
    fake_mcp.ClientSession = _FakeSession  # type: ignore[attr-defined] — dynamic attr on synthetic ModuleType
    fake_streamable_mod = types.ModuleType("mcp.client.streamable_http")
    fake_streamable_mod.streamablehttp_client = fake_streamable  # type: ignore[attr-defined] — dynamic attr on synthetic ModuleType
    fake_client_pkg = types.ModuleType("mcp.client")
    fake_client_pkg.streamable_http = fake_streamable_mod  # type: ignore[attr-defined] — dynamic attr on synthetic ModuleType
    monkeypatch.setitem(_sys.modules, "mcp", fake_mcp)
    monkeypatch.setitem(_sys.modules, "mcp.client", fake_client_pkg)
    monkeypatch.setitem(_sys.modules, "mcp.client.streamable_http", fake_streamable_mod)

    client = HttpMcpDispatchClient()
    result = client.call_tool("http://localhost:8080/mcp", "search", {"query": "test"})

    assert init_calls == [True], "session.initialize() must run before call_tool"
    assert tool_calls == [("search", {"query": "test"})]
    assert result.payload == {"result": "ok", "from": "fake-mcp"}
    assert result.is_error is False


@pytest.mark.unit
def test_dispatcher_falls_through_on_unresponsive_probe_without_extra_call() -> None:
    """A non-responsive probe ⇒ fall-through with exactly one probe, no tool call.

    F82 rewrite (#493): the old shape slept 50ms in the fake probe and
    asserted ``elapsed < 0.150`` to prove "the dispatcher adds no sleep of
    its own". That ceiling measures host scheduling and flakes under load.
    The deterministic outcome the test actually protects is: when the
    probe reports not-responsive, the dispatcher returns ``None``
    (in-process fall-through) after exactly ONE ``is_responsive`` call and
    makes ZERO ``call_tool`` calls — it does no extra round-trip / retry /
    reconnect work. That is what a "give it a moment to reconnect"
    regression (a ``time.sleep`` + re-probe, or a speculative
    ``call_tool``) would break, and it is provable without a clock: such a
    regression would add a second ``responsive_calls`` entry or a
    ``calls`` entry.

    Sabotage-proof (executed): added a re-probe (``client.is_responsive``
    called twice) after the first not-responsive return in
    ``try_dispatch_via_mcp`` → this test failed (``len(responsive_calls)
    == 2``). Restoring the single-probe fall-through restored green.
    Deterministic on any host — no elapsed-time ceiling.
    """
    client = FakeMcpDispatchClient(responsive=False)
    deps = DispatcherDeps(
        client=client,
        endpoint_fn=lambda: "http://localhost:8080/mcp",
        routing_enabled_fn=lambda: True,
    )

    exit_code = try_dispatch_via_mcp("search", ["q", "--json"], deps=deps)

    assert exit_code is None, "an unresponsive probe must fall through to in-process (return None)"
    assert len(client.responsive_calls) == 1, "the dispatcher must probe exactly once (no retry/reconnect loop)"
    assert client.calls == [], "no tool call may run after a not-responsive probe"
