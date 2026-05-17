"""Unit tests for ``async_tool_handler`` — sync→async tool wrapper.

The wrapper resolves #177: pre-fix, the MCP server registered every
tool as a SYNC function. FastMCP runs sync tool handlers in a small
threadpool, but the GIL plus per-call work meant five concurrent
``/mcp`` calls saw the first complete in 1.5 s and the other four
queue to ~4.25 s each.

These tests verify ``async_tool_handler``:

  - Wraps a sync handler into a coroutine.
  - Returns the handler's dict on success.
  - Maps exceptions to the same ``{"error": ...}`` envelope as
    ``wrap_tool_errors``.
  - **Schedules concurrent calls onto separate threads** so a slow
    handler doesn't block subsequent ones (#177).

Tested through public surface only — no private symbols, no
@patch / monkeypatch.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time

import pytest

from kairix.agents.mcp.errors import async_tool_handler


@pytest.mark.unit
def test_returns_a_coroutine_function() -> None:
    """A sync handler decorated with async_tool_handler becomes async."""

    def sync_impl(x: int) -> dict[str, int]:
        return {"value": x}

    wrapped = async_tool_handler(sync_impl)
    assert inspect.iscoroutinefunction(wrapped)


@pytest.mark.unit
def test_success_returns_handlers_dict() -> None:
    def sync_impl(x: int) -> dict[str, int]:
        return {"value": x * 2}

    wrapped = async_tool_handler(sync_impl)
    result = asyncio.run(wrapped(21))
    assert result == {"value": 42}


@pytest.mark.unit
def test_exception_is_mapped_to_error_envelope() -> None:
    """Exceptions in the sync handler must NOT propagate — same contract as wrap_tool_errors."""

    def boom(_x: int) -> dict[str, int]:
        raise RuntimeError("kapow")

    wrapped = async_tool_handler(boom)
    result = asyncio.run(wrapped(1))
    assert result == {"error": "RuntimeError: kapow"}


@pytest.mark.unit
def test_concurrent_calls_run_in_parallel(caplog: pytest.LogCaptureFixture) -> None:
    """Five concurrent calls to a slow handler should NOT serialize.

    The fix's load-bearing contract: pre-fix the wallclock time of
    five concurrent 100 ms calls was ~500 ms (serial); post-fix it
    must be roughly the duration of one call (~100-200 ms with
    thread-startup overhead). We assert wallclock < 4x the per-call
    cost — a generous bound that still excludes serial execution
    (which would be 5x).
    """
    per_call_ms = 80
    n_calls = 5

    def slow_handler(call_id: int) -> dict[str, int]:
        time.sleep(per_call_ms / 1000)
        return {"id": call_id}

    wrapped = async_tool_handler(slow_handler)

    async def fire_all() -> list[dict[str, int]]:
        return await asyncio.gather(*(wrapped(i) for i in range(n_calls)))

    started = time.monotonic()
    results = asyncio.run(fire_all())
    elapsed_ms = (time.monotonic() - started) * 1000

    assert len(results) == n_calls
    assert {r["id"] for r in results} == set(range(n_calls))

    serial_lower_bound_ms = per_call_ms * n_calls  # 400 ms
    parallel_upper_bound_ms = per_call_ms * 4  # 320 ms — still allows ~3x overhead
    assert elapsed_ms < parallel_upper_bound_ms, (
        f"expected parallel execution (< {parallel_upper_bound_ms} ms), "
        f"got {elapsed_ms:.0f} ms (serial would be ~{serial_lower_bound_ms} ms)"
    )


@pytest.mark.unit
def test_handler_metadata_is_preserved() -> None:
    """``functools.wraps`` keeps the original handler's name and docstring,
    so FastMCP's tool-registration machinery sees the right signature.
    """

    def search(query: str) -> dict[str, str]:
        """Search your knowledge store — finds the best answers."""
        return {"q": query}

    wrapped = async_tool_handler(search)
    assert wrapped.__name__ == "search"
    assert wrapped.__doc__ is not None
    assert "knowledge store" in wrapped.__doc__


@pytest.mark.unit
def test_keyword_arguments_are_forwarded() -> None:
    """Tool handlers receive kwargs from FastMCP's dispatcher; verify they pass through."""

    def handler(query: str, agent: str | None = None, budget: int = 3000) -> dict[str, object]:
        return {"q": query, "a": agent, "b": budget}

    wrapped = async_tool_handler(handler)
    result = asyncio.run(wrapped(query="ping", agent="builder", budget=1500))
    assert result == {"q": "ping", "a": "builder", "b": 1500}


@pytest.mark.unit
def test_exception_logged_with_handler_name(caplog: pytest.LogCaptureFixture) -> None:
    """The error log message names the failing handler so observability can group by tool."""

    def search(_query: str) -> dict[str, str]:
        raise ValueError("bad query")

    wrapped = async_tool_handler(search)

    with caplog.at_level(logging.WARNING, logger="kairix.agents.mcp.errors"):
        result = asyncio.run(wrapped("anything"))

    assert result["error"] == "ValueError: bad query"
    assert any("search" in rec.message for rec in caplog.records), (
        f"expected handler name 'search' in log; got: {[r.message for r in caplog.records]}"
    )
