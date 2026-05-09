"""Regression tests for agent_memory_path — guards against the path-doubling
bug reported in GH #67 / #93 and surfaced again in the 2026-05-02 dogfood.

The original bug: when the user passed --memory-root /path/to/04-Agent-Knowledge,
the brief CLI set KAIRIX_AGENT_MEMORY_ROOT to that, then agent_memory_path
appended /{agent}/memory — correct. But when the user passed the full
.../{agent}/memory path, the function appended /{agent}/memory again,
producing .../{agent}/memory/{agent}/memory and a missing-memory error.

The fix detects the suffix and accepts it as-is. Tests pass an explicit
``env=`` mapping to ``agent_memory_path`` rather than mutating the process
environment.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kairix.paths import agent_memory_path


@pytest.mark.unit
def test_bare_root_appends_agent_and_memory() -> None:
    result = agent_memory_path(
        "shape",
        env={"KAIRIX_AGENT_MEMORY_ROOT": "/data/documents/04-Agent-Knowledge"},
    )
    assert result == Path("/data/documents/04-Agent-Knowledge/shape/memory")


@pytest.mark.unit
def test_trailing_slash_root_appends_agent_and_memory() -> None:
    """Trailing slash on the override should not change the resolved path."""
    result = agent_memory_path(
        "shape",
        env={"KAIRIX_AGENT_MEMORY_ROOT": "/data/documents/04-Agent-Knowledge/"},
    )
    assert result == Path("/data/documents/04-Agent-Knowledge/shape/memory")


@pytest.mark.unit
def test_full_path_with_agent_memory_suffix_accepted_as_is() -> None:
    """The dogfood failure mode: caller passes .../shape/memory expecting it
    to be the memory dir. Function detects and returns as-is, no doubling."""
    result = agent_memory_path(
        "shape",
        env={"KAIRIX_AGENT_MEMORY_ROOT": "/data/documents/04-Agent-Knowledge/shape/memory"},
    )
    # The critical assertion: NO doubled segment. Three proofs of the same
    # property — explicit equality, segment count, and the absence of the
    # buggy substring.
    assert result == Path("/data/documents/04-Agent-Knowledge/shape/memory")
    assert "shape/memory/shape/memory" not in str(result)
    assert str(result).count("/shape/memory") == 1


@pytest.mark.unit
def test_full_path_with_agent_memory_suffix_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The misuse path is friendly but not silent — it emits a WARNING so the
    operator can fix their --memory-root invocation."""
    import logging

    with caplog.at_level(logging.WARNING, logger="kairix.paths"):
        agent_memory_path(
            "shape",
            env={"KAIRIX_AGENT_MEMORY_ROOT": "/data/documents/04-Agent-Knowledge/shape/memory"},
        )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("path-doubling" in r.getMessage() for r in warnings)


# test_no_override_falls_through_to_document_root removed: that path tests
# the document_root() fallback chain which itself reads KAIRIX_DOCUMENT_ROOT.
# Coverage of document_root() resolution lives in tests/test_paths.py and
# is exercised end-to-end by integration tests of the brief / search CLIs
# without needing process-env mutation here.


@pytest.mark.unit
def test_different_agents_get_different_directories() -> None:
    env = {"KAIRIX_AGENT_MEMORY_ROOT": "/data/documents/04-Agent-Knowledge"}
    assert agent_memory_path("shape", env=env) != agent_memory_path("builder", env=env)
