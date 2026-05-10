"""
kairix.agents.mcp.transport — ASGI transport composer for the kairix MCP server.

This is the only module in the codebase that knows how FastMCP's transport
apps (streamable HTTP, SSE) are mounted. CLI entry points and tests construct
the Starlette app via :func:`build_mcp_app`.

Sprint 19 motivation
--------------------
Pre-Sprint 19 the MCP server only exposed an SSE transport on ``/sse``. The
2026-05-02 dogfood failure (every ``mcp-kairix__*`` tool returning
``-32602 Invalid request parameters``) traced back to the gateway dropping
idle SSE connections. Streamable HTTP turns every tool call into a normal
HTTP request/response, removing the long-lived-connection failure mode by
construction. We mount the streamable transport at ``/mcp`` and keep
``/sse`` mounted for back-compat with older clients.

Design
------
- Public surface is a single function, :func:`build_mcp_app`.
- Helpers are ``_``-prefixed and treated as private implementation detail.
- The composer never starts a server; it only returns a Starlette app.
- ``starlette`` is a transitive dependency of ``mcp>=1.20`` (declared in the
  ``agents`` extra in ``pyproject.toml``).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

# Module-level start timestamp captured on first build_mcp_app() call.
# This is *implementation* of the public function — not exposed elsewhere.
_started_at: float | None = None


def _ensure_started_at() -> float:
    """Return the process start time, capturing it on first call."""
    global _started_at
    if _started_at is None:
        _started_at = time.monotonic()
    return _started_at


def _make_healthz_route(
    path: str,
    readiness_check: Callable[[], bool] | None,
) -> Route:
    """Basic liveness probe: ``{"ready": bool, "uptime_s": int}``.

    ``readiness_check`` (when provided) is the legacy boolean signal for
    whether kairix has finished cold-starting. It is intentionally
    coarse — for layered checks (secrets, vector search, BM25) use
    ``/healthz/ready``.
    """
    started_at = _ensure_started_at()

    async def healthz(_request: Request) -> JSONResponse:
        ready = bool(readiness_check()) if readiness_check is not None else True
        uptime_s = int(time.monotonic() - started_at)
        return JSONResponse({"ready": ready, "uptime_s": uptime_s})

    return Route(path, healthz, methods=["GET"])


def _make_ready_route(
    path: str,
    capability_probe: Callable[[], dict[str, Any]] | None,
) -> Route:
    """Layered readiness probe: ``{"live": true, "ready": bool, "checks": {...}}``.

    Resolves the gap from #167 where ``/healthz`` reported ``ready=true``
    while vector search was non-functional due to missing secrets. The
    probe runs ``capability_probe()`` (if provided) and surfaces the
    structured result. A probe that lists ``secrets_loaded=False`` or
    ``vector_search_capable=False`` is the actionable signal an
    operator needs to triage a degraded deployment.

    Response shape (when ``capability_probe`` is wired):

    .. code-block:: json

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

    HTTP status is always 200 (load-balancer probes should treat this as
    a JSON-content health check, not as an HTTP gate). The ``ready``
    field is the boolean to act on.

    When ``capability_probe`` is None the probe degrades to the same
    semantics as ``/healthz`` so this endpoint is always wired and an
    operator never gets a 404.
    """
    started_at = _ensure_started_at()

    async def healthz_ready(_request: Request) -> JSONResponse:
        uptime_s = int(time.monotonic() - started_at)
        if capability_probe is None:
            return JSONResponse({"live": True, "ready": True, "uptime_s": uptime_s, "checks": {}})
        try:
            checks = capability_probe()
        # Probe authors are encouraged to handle their own exceptions and
        # report them in ``detail``. This guard is defensive: if the probe
        # itself raises, we surface that as a structured failure rather
        # than crashing the request.
        except Exception as exc:
            return JSONResponse(
                {
                    "live": True,
                    "ready": False,
                    "uptime_s": uptime_s,
                    "checks": {"probe_error": str(exc)},
                }
            )
        ready = bool(checks.get("ready", _derive_ready_from_checks(checks)))
        return JSONResponse(
            {
                "live": True,
                "ready": ready,
                "uptime_s": uptime_s,
                "checks": checks,
            }
        )

    return Route(path, healthz_ready, methods=["GET"])


def _derive_ready_from_checks(checks: dict[str, Any]) -> bool:
    """Default readiness: ALL boolean keys named ``*_capable`` /
    ``*_loaded`` must be True. Lets callers omit a top-level ``ready``
    field and have it derived from the granular checks.
    """
    relevant = [
        v for k, v in checks.items() if isinstance(v, bool) and (k.endswith("_capable") or k.endswith("_loaded"))
    ]
    return all(relevant) if relevant else True


def _apply_settings(server: Any) -> None:
    """Set stateless_http and json_response on server.settings if present.

    Defensive: FastMCP exposes ``settings`` as a Pydantic model in mcp>=1.20,
    but we don't want to crash if a future version reshapes the API.
    """
    settings = getattr(server, "settings", None)
    if settings is None:
        return
    try:
        settings.stateless_http = True
        settings.json_response = True
    except (AttributeError, TypeError):  # pragma: no cover — defensive
        return


def build_mcp_app(
    server: Any,
    *,
    with_sse: bool = True,
    sse_mount_path: str = "/sse",
    healthz_path: str = "/healthz",
    healthz_ready_path: str = "/healthz/ready",
    readiness_check: Callable[[], bool] | None = None,
    capability_probe: Callable[[], dict[str, Any]] | None = None,
) -> Starlette:
    """Compose the kairix MCP ASGI app.

    - Mounts the streamable HTTP transport at ``/mcp`` (FastMCP default).
    - If ``with_sse``, also mounts the legacy SSE transport at
      ``sse_mount_path`` for back-compat.
    - Adds two health endpoints:
        - ``/healthz`` — basic liveness, ``{"ready": bool, "uptime_s": int}``.
          Back-compat with the existing endpoint.
        - ``/healthz/ready`` — layered readiness, runs
          ``capability_probe()`` and reports per-capability detail.
          Resolves the #167 gap where ``/healthz`` returned
          ``ready=true`` while vector search was broken.

    The composer is the only place in the codebase that knows about
    FastMCP's transport apps. CLI entry points construct via this function;
    tests construct via this function with a fake server and optional
    probe callbacks.

    Args:
        server: FastMCP instance. Typed loosely because the ``mcp`` package
            does not publish public stubs for these methods.
        with_sse: If True, mount the legacy SSE transport.
        sse_mount_path: Mount path passed to ``server.sse_app``.
        healthz_path: Path for the basic liveness probe route.
        healthz_ready_path: Path for the layered readiness probe route.
        readiness_check: Optional callable used to populate the ``ready``
            field of the basic ``/healthz`` JSON body. Called on every
            request.
        capability_probe: Optional callable returning a dict of granular
            capability checks (``secrets_loaded``,
            ``vector_search_capable``, ``bm25_search_capable``, plus a
            ``detail`` map). Wired into ``/healthz/ready``.

    Returns:
        A composed :class:`starlette.applications.Starlette` instance with
        the streamable HTTP routes, optionally the SSE routes, and the
        liveness + readiness probe routes.
    """
    _apply_settings(server)

    streamable_app: Starlette = server.streamable_http_app()
    routes: list[Any] = list(streamable_app.routes)

    if with_sse:
        sse_app: Starlette = server.sse_app(mount_path=sse_mount_path)
        routes.extend(sse_app.routes)

    routes.append(_make_healthz_route(healthz_path, readiness_check))
    routes.append(_make_ready_route(healthz_ready_path, capability_probe))

    # Preserve the streamable app's lifespan so FastMCP's session manager
    # starts/stops correctly when the composed app is served.
    lifespan = getattr(streamable_app.router, "lifespan_context", None)
    if lifespan is not None:
        return Starlette(routes=routes, lifespan=lifespan)
    return Starlette(routes=routes)


__all__ = ["build_mcp_app"]
