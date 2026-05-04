"""Regression tests for agent_memory_path — guards against the path-doubling
bug reported in GH #67 / #93 and surfaced again in the 2026-05-02 dogfood.

The original bug: when the user passed --memory-root /path/to/04-Agent-Knowledge,
the brief CLI set KAIRIX_AGENT_MEMORY_ROOT to that, then agent_memory_path
appended /{agent}/memory — correct. But when the user passed the full
.../{agent}/memory path, the function appended /{agent}/memory again,
producing .../{agent}/memory/{agent}/memory and a missing-memory error.

The fix detects the suffix and accepts it as-is. These tests cover all
three input shapes from the sprint plan.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kairix.paths import agent_memory_path


@pytest.mark.unit
def test_bare_root_appends_agent_and_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAIRIX_AGENT_MEMORY_ROOT", "/data/documents/04-Agent-Knowledge")
    result = agent_memory_path("shape")
    assert result == Path("/data/documents/04-Agent-Knowledge/shape/memory")


@pytest.mark.unit
def test_trailing_slash_root_appends_agent_and_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trailing slash on the override should not change the resolved path."""
    monkeypatch.setenv("KAIRIX_AGENT_MEMORY_ROOT", "/data/documents/04-Agent-Knowledge/")
    result = agent_memory_path("shape")
    assert result == Path("/data/documents/04-Agent-Knowledge/shape/memory")


@pytest.mark.unit
def test_full_path_with_agent_memory_suffix_accepted_as_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dogfood failure mode: caller passes .../shape/memory expecting it
    to be the memory dir. Function detects and returns as-is, no doubling."""
    monkeypatch.setenv(
        "KAIRIX_AGENT_MEMORY_ROOT",
        "/data/documents/04-Agent-Knowledge/shape/memory",
    )
    result = agent_memory_path("shape")
    # The critical assertion: NO doubled segment. Three proofs of the same
    # property — explicit equality, segment count, and the absence of the
    # buggy substring.
    assert result == Path("/data/documents/04-Agent-Knowledge/shape/memory")
    assert "shape/memory/shape/memory" not in str(result)
    assert str(result).count("/shape/memory") == 1


@pytest.mark.unit
def test_full_path_with_agent_memory_suffix_logs_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The misuse path is friendly but not silent — it emits a WARNING so the
    operator can fix their --memory-root invocation."""
    monkeypatch.setenv(
        "KAIRIX_AGENT_MEMORY_ROOT",
        "/data/documents/04-Agent-Knowledge/shape/memory",
    )
    import logging

    with caplog.at_level(logging.WARNING, logger="kairix.paths"):
        agent_memory_path("shape")

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("path-doubling" in r.getMessage() for r in warnings)


@pytest.mark.unit
def test_no_override_falls_through_to_document_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no override, the function uses document_root() / 04-Agent-Knowledge / agent / memory."""
    monkeypatch.delenv("KAIRIX_AGENT_MEMORY_ROOT", raising=False)
    monkeypatch.setenv("KAIRIX_DOCUMENT_ROOT", "/some/vault")

    # Clear paths cache so KAIRIX_DOCUMENT_ROOT takes effect
    from kairix.paths import clear_cache

    clear_cache()
    result = agent_memory_path("shape")
    assert result == Path("/some/vault/04-Agent-Knowledge/shape/memory")
    clear_cache()


@pytest.mark.unit
def test_different_agents_get_different_directories(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAIRIX_AGENT_MEMORY_ROOT", "/data/documents/04-Agent-Knowledge")
    assert agent_memory_path("shape") != agent_memory_path("builder")
