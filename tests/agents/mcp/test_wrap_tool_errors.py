"""Unit tests for the public wrap_tool_errors callable.

Tested through public surface: wrap a handler, call it, assert the dict
shape. No private symbol imports, no @patch, no monkeypatch.
"""

from __future__ import annotations

import logging

import pytest

from kairix.agents.mcp.errors import wrap_tool_errors


@pytest.mark.unit
def test_success_passes_through_unchanged() -> None:
    @wrap_tool_errors
    def handler(x: int) -> dict:
        return {"value": x * 2}

    assert handler(21) == {"value": 42}


@pytest.mark.unit
def test_exception_becomes_error_dict() -> None:
    @wrap_tool_errors
    def handler() -> dict:
        raise RuntimeError("boom")

    result = handler()
    assert "error" in result
    assert "RuntimeError" in result["error"]
    assert "boom" in result["error"]


@pytest.mark.unit
def test_exception_class_preserved_in_error_message() -> None:
    """Observability needs to group by error class — verify the class name lands."""

    class _CustomError(ValueError):
        pass

    @wrap_tool_errors
    def handler() -> dict:
        raise _CustomError("specific failure")

    result = handler()
    assert "_CustomError" in result["error"]
    assert "specific failure" in result["error"]


@pytest.mark.unit
def test_exception_logged_at_warning_with_traceback(caplog: pytest.LogCaptureFixture) -> None:
    @wrap_tool_errors
    def handler() -> dict:
        raise RuntimeError("logged")

    with caplog.at_level(logging.WARNING, logger="kairix.agents.mcp.errors"):
        handler()

    assert any(record.levelno == logging.WARNING for record in caplog.records)
    # Traceback present means exc_info captured something
    assert any(record.exc_info is not None for record in caplog.records)


@pytest.mark.unit
def test_handler_name_and_doc_preserved() -> None:
    @wrap_tool_errors
    def my_named_handler() -> dict:
        """Docstring of the handler."""
        return {}

    assert my_named_handler.__name__ == "my_named_handler"
    assert my_named_handler.__doc__ == "Docstring of the handler."


@pytest.mark.unit
def test_kwargs_forwarded() -> None:
    @wrap_tool_errors
    def handler(*, name: str, count: int = 1) -> dict:
        return {"name": name, "count": count}

    assert handler(name="kairix", count=3) == {"name": "kairix", "count": 3}


@pytest.mark.unit
def test_keyboard_interrupt_not_caught() -> None:
    """BaseException-level interrupts should propagate, not be swallowed.

    wrap_tool_errors catches Exception, not BaseException, so KeyboardInterrupt
    and SystemExit pass through. This matters for CTRL-C during local dev and
    graceful shutdown signals in production.
    """

    @wrap_tool_errors
    def handler() -> dict:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        handler()


@pytest.mark.unit
def test_nested_exception_message_preserved() -> None:
    """Exceptions raised from a `raise X from Y` chain — outer message lands."""

    @wrap_tool_errors
    def handler() -> dict:
        try:
            raise ValueError("inner cause")
        except ValueError as e:
            raise RuntimeError("outer wrapper") from e

    result = handler()
    assert "RuntimeError" in result["error"]
    assert "outer wrapper" in result["error"]
