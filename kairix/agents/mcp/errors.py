"""Error-envelope + async-offload wrappers for MCP tool handlers.

FastMCP's generic error mapper converts any exception escaping a tool
handler into a JSON-RPC ``-32602 Invalid request parameters`` response,
which masks the real cause. The 2026-05-02 dogfood report observed every
``mcp-kairix__*`` tool returning -32602 even though the underlying
errors were diverse (Neo4j unavailable, LLM rate-limited, transport
closed) — none of them parameter-validation failures.

This module exposes two callables:

  - ``wrap_tool_errors`` — sync handler → sync handler with error
    envelope. Catches every exception, logs it with traceback, and
    returns a structured ``{"error": "<class>: <message>"}`` dict —
    bypassing FastMCP's ``-32602`` mapper because the handler now
    returns successfully with an error payload.

  - ``async_tool_handler`` — sync handler → async handler that offloads
    the sync work to the default asyncio threadpool via
    ``asyncio.to_thread``. The error envelope is applied inside the
    threaded call. Concurrent ``/mcp`` requests no longer queue behind
    each other on the event loop — resolves #177.

Tested through public surface only: register a handler that raises,
call it, observe the dict.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

_F = TypeVar("_F", bound=Callable[..., dict[str, Any]])


def wrap_tool_errors(handler: _F) -> _F:
    """Wrap an MCP tool handler so escaped exceptions become error dicts.

    The wrapped handler:
      - Returns the original handler's dict on success.
      - On any exception, logs at WARNING with traceback and returns
        ``{"error": "<ExceptionClass>: <message>"}``. The exception
        class name is preserved so observability can group by error type.

    The wrapper preserves the handler's name and docstring via functools.wraps
    so FastMCP's tool-registration machinery sees the original signature.
    """

    @functools.wraps(handler)
    def _wrapped(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return handler(*args, **kwargs)
        except Exception as exc:
            logger.warning(
                "mcp tool %s raised %s: %s",
                getattr(handler, "__name__", "<unknown>"),
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            return {"error": f"{type(exc).__name__}: {exc}"}

    return _wrapped  # type: ignore[return-value]


def async_tool_handler(
    handler: Callable[..., dict[str, Any]],
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Convert a sync MCP tool handler into an async one that offloads.

    The returned coroutine:
      1. Captures the call from FastMCP's tool dispatcher.
      2. Calls ``asyncio.to_thread`` to run the sync handler in the
         default ThreadPoolExecutor (8 workers in CPython 3.12 by
         default; configurable via ``loop.set_default_executor``).
      3. Returns the handler's dict, OR a structured error envelope
         when the handler raises (the ``wrap_tool_errors`` semantics
         are applied inside the thread, before re-entering the loop).

    Concurrent ``/mcp`` requests are scheduled onto the event loop and
    each tool call's blocking work happens on its own thread, so a
    long-running search no longer blocks subsequent calls.

    Sync work in the handlers is still subject to the GIL — but threads
    interleave correctly when waiting on I/O (HTTP to OpenRouter,
    SQLite reads, Neo4j round-trips), which is most of any tool call's
    elapsed time. CPU-bound stages (BM25 ranking, vector similarity,
    rerank) still serialize per-call but no longer block the event
    loop. See #177.
    """
    safe = wrap_tool_errors(handler)

    @functools.wraps(handler)
    async def _wrapped(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return await asyncio.to_thread(safe, *args, **kwargs)

    return _wrapped
