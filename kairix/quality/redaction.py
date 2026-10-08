"""Describe an exception for persisted output without leaking its message.

Exceptions raised by providers, transports, retrieval backends and other
external systems can carry API keys, auth headers, request payloads or
retrieved content in their *message*. Anything kairix persists — benchmark
and eval JSON, probe / soak reports, per-query result rows — must therefore
record only the exception's **class name**, which is code-defined and still
machine-readable. Every such site formats through :func:`describe_exception`
so the rule lives in one place.

Keep the full exception only in a local log line, and only where the code
already logs it.
"""

from __future__ import annotations


def describe_exception(exc: BaseException) -> str:
    """Return ``"raised <ExceptionClass>"`` — never the exception message."""
    return f"raised {type(exc).__name__}"


__all__ = ["describe_exception"]
