"""Error-envelope wrapper for MCP tool handlers.

FastMCP's generic error mapper converts any exception escaping a tool
handler into a JSON-RPC ``-32602 Invalid request parameters`` response,
which masks the real cause. The 2026-05-02 dogfood report observed every
``mcp-kairix__*`` tool returning -32602 even though the underlying
errors were diverse (Neo4j unavailable, LLM rate-limited, transport
closed) — none of them parameter-validation failures.

This module exposes a single public callable, ``wrap_tool_errors``,
applied to each ``@server.tool()``-registered handler. It catches every
exception, logs it with traceback, and returns a structured
``{"error": "<class>: <message>", ...}`` dict — bypassing FastMCP's
``-32602`` mapper because the handler now returns successfully with an
error payload.

Tested through public surface only: register a handler that raises,
call it, observe the dict.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
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
