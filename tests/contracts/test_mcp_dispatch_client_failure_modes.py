"""F68 failure-mode contract for the ``McpDispatchClient`` Protocol.

The CLI's warm-MCP shortcut (``try_dispatch_via_mcp``) is best-effort:
when the MCP server is unreachable, or the tool call itself fails, the
CLI must fall through to its in-process path (``None``) and print
nothing — never crash, never emit a half-rendered envelope.

F43 parity: ONE body per method runs over the real
``HttpMcpDispatchClient`` and the canonical ``FakeMcpDispatchClient``.
The real client is driven into failure without any network: a
scheme-less endpoint makes ``requests`` / ``httpx`` reject the URL
before a socket is opened.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from kairix.agents.mcp.client_dispatcher import (
    DispatcherDeps,
    HttpMcpDispatchClient,
    McpDispatchClient,
    McpToolResult,
    try_dispatch_via_mcp,
)
from tests.fakes import FakeMcpDispatchClient

pytestmark = pytest.mark.contract

# Scheme-less on purpose: both HTTP stacks reject it locally (no DNS, no
# connect), which is the deterministic "endpoint unreachable" shape.
_UNREACHABLE_ENDPOINT = "not-a-url"
_SEARCH_ARGV = ["rollout plan", "--json"]


class _ForcedResponsive:
    """Wraps a client so the readiness probe passes and ``call_tool`` runs.

    Isolates the ``call_tool`` failure path: without it the real client's
    own (failing) probe would short-circuit the dispatcher before the
    tool call is attempted.
    """

    def __init__(self, inner: McpDispatchClient) -> None:
        self._inner = inner

    def is_responsive(self, endpoint: str, timeout_s: float) -> bool:
        del endpoint, timeout_s
        return True

    def call_tool(self, endpoint: str, tool_name: str, kwargs: dict[str, Any]) -> McpToolResult:
        return self._inner.call_tool(endpoint, tool_name, kwargs)


def _deps(client: McpDispatchClient) -> DispatcherDeps:
    return DispatcherDeps(
        client=client,
        endpoint_fn=lambda: _UNREACHABLE_ENDPOINT,
        routing_enabled_fn=lambda: True,
    )


@pytest.mark.parametrize(
    "name,factory",
    [
        ("real", HttpMcpDispatchClient),
        ("fake", lambda: FakeMcpDispatchClient(responsive=False)),
    ],
)
def test_is_responsive_unavailable_server_falls_through_to_in_process(
    name: str, factory: Callable[[], McpDispatchClient], capsys: pytest.CaptureFixture[str]
) -> None:
    """``unavailable``: an unreachable MCP server probes as not-responsive
    and the dispatcher returns ``None`` (run in-process) with no output.

    Sabotage proof (executed): make ``HttpMcpDispatchClient.is_responsive``
    return ``True`` from its request-failure ``except`` branch → the ``real`` case fails
    (the probe claims a dead endpoint is up). Restored.
    """
    client = factory()
    assert client.is_responsive(_UNREACHABLE_ENDPOINT, 0.1) is False, name
    assert try_dispatch_via_mcp("search", list(_SEARCH_ARGV), deps=_deps(client)) is None
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "name,factory",
    [
        ("real", HttpMcpDispatchClient),
        ("fake", lambda: FakeMcpDispatchClient(raise_on_call=ConnectionResetError("MCP session dropped"))),
    ],
)
def test_call_tool_raises_dispatcher_falls_through_without_output(
    name: str, factory: Callable[[], McpDispatchClient], capsys: pytest.CaptureFixture[str]
) -> None:
    """``raises``: a tool call that fails raises an ``Exception``
    subclass (the real client's anyio ``ExceptionGroup`` included), so
    the dispatcher's fallback catches it — ``None``, nothing printed.

    Sabotage proof (executed): narrow the dispatcher's ``except
    Exception`` around ``client.call_tool`` to ``except
    ConnectionResetError`` → the ``real`` case fails (the ExceptionGroup
    escapes and would crash the CLI). Restored.
    """
    client = factory()
    result = try_dispatch_via_mcp("search", list(_SEARCH_ARGV), deps=_deps(_ForcedResponsive(client)))
    assert result is None, name
    assert capsys.readouterr().out == ""
