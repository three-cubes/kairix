"""Unit tests for the MCP transport composer (Sprint 19 WS1-2).

We use a fake FastMCP-shaped object (``streamable_http_app``, ``sse_app``,
``settings``) so the tests run without the ``mcp`` package installed.
Per CONSTRAINTS.md G6, we do not import private symbols from
``kairix.agents.mcp.transport`` and we do not use ``unittest.mock.patch``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

# Skip the entire module when the optional [agents] extras (mcp + starlette
# transitively) aren't installed. CI's contract stage runs the base deps only;
# the agents-extras tests run in stages that install the full extras.
starlette = pytest.importorskip("starlette")
from starlette.applications import Starlette  # noqa: E402
from starlette.responses import PlainTextResponse, Response  # noqa: E402
from starlette.routing import Mount, Route  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from kairix.agents.mcp.transport import build_mcp_app  # noqa: E402

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fakes — minimal FastMCP-shaped surface
# ---------------------------------------------------------------------------


class _FakeSettings:
    """Stand-in for FastMCP's pydantic ``Settings`` model."""

    def __init__(self) -> None:
        self.stateless_http: bool = False
        self.json_response: bool = False


class _FakeFastMCP:
    """Just enough surface for ``build_mcp_app`` to compose against.

    - ``streamable_http_app()`` returns a tiny Starlette app with one route at
      ``/mcp`` whose handler records that it was invoked.
    - ``sse_app(mount_path)`` returns a tiny Starlette app with one route at
      ``mount_path``.
    - ``settings`` is a mutable object exposing ``stateless_http`` and
      ``json_response``.
    """

    def __init__(self, *, sse_route_path: str | None = None) -> None:
        self.settings = _FakeSettings()
        self._streamable_calls: int = 0
        self._sse_route_path = sse_route_path

    @property
    def streamable_calls(self) -> int:
        return self._streamable_calls

    def streamable_http_app(self) -> Starlette:
        async def handler(_request: Any) -> Response:
            self._streamable_calls += 1
            return PlainTextResponse("streamable-ok")

        return Starlette(routes=[Route("/mcp", handler, methods=["GET", "POST"])])

    def sse_app(self, mount_path: str | None = None) -> Starlette:
        path = mount_path or self._sse_route_path or "/sse"

        async def handler(_request: Any) -> Response:
            return PlainTextResponse("sse-ok")

        return Starlette(routes=[Route(path, handler, methods=["GET"])])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _route_paths(app: Starlette) -> list[str]:
    """Return concrete paths for Route entries; mount prefixes for Mount entries."""
    paths: list[str] = []
    for route in app.routes:
        if isinstance(route, Route):
            paths.append(route.path)
        elif isinstance(route, Mount):
            paths.append(route.path)
    return paths


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_returns_starlette_instance() -> None:
    server = _FakeFastMCP()
    app = build_mcp_app(server)
    assert isinstance(app, Starlette)


@pytest.mark.unit
def test_healthz_default_returns_ready_true_with_uptime() -> None:
    server = _FakeFastMCP()
    app = build_mcp_app(server)

    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert isinstance(body["uptime_s"], int)
    assert body["uptime_s"] >= 0


@pytest.mark.unit
def test_healthz_readiness_check_false_returns_ready_false() -> None:
    server = _FakeFastMCP()

    not_ready: Callable[[], bool] = lambda: False  # noqa: E731 — concise test stub
    app = build_mcp_app(server, readiness_check=not_ready)

    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is False
    assert isinstance(body["uptime_s"], int)


@pytest.mark.unit
def test_with_sse_false_omits_sse_routes() -> None:
    server = _FakeFastMCP()
    app = build_mcp_app(server, with_sse=False)

    paths = _route_paths(app)
    assert "/mcp" in paths
    assert "/healthz" in paths
    assert "/sse" not in paths
    # And no path should match the configured SSE mount path either.
    assert all(not p.startswith("/sse") for p in paths)


@pytest.mark.unit
def test_with_sse_true_includes_both_mcp_and_sse_routes() -> None:
    server = _FakeFastMCP()
    app = build_mcp_app(server, with_sse=True)

    paths = _route_paths(app)
    assert "/mcp" in paths
    assert "/sse" in paths
    assert "/healthz" in paths


@pytest.mark.unit
def test_settings_are_set_for_stateless_json_transport() -> None:
    server = _FakeFastMCP()
    assert server.settings.stateless_http is False
    assert server.settings.json_response is False

    build_mcp_app(server)

    assert server.settings.stateless_http is True
    assert server.settings.json_response is True


@pytest.mark.unit
def test_streamable_route_reaches_underlying_handler() -> None:
    server = _FakeFastMCP()
    app = build_mcp_app(server)

    with TestClient(app) as client:
        response = client.get("/mcp")

    assert response.status_code == 200
    assert response.text == "streamable-ok"
    assert server.streamable_calls == 1
