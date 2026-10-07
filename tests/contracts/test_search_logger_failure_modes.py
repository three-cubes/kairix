"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`SearchLogger`.

Two methods (``log_search``, ``log_query``). Failure surface:

  * ``raises`` — a sink failure (disk full, path is a directory,
    permission denied) raised INSIDE the logger is contained: the call
    returns normally and nothing is recorded. The shipped
    :class:`kairix.core.search.logger.JsonlSearchLogger` documents
    "never raises — search must not break because logging broke", so
    that is the contract both implementations are held to here.
  * ``returns_empty`` — log_search / log_query return ``None``; an empty
    event is still captured as exactly one record carrying no caller
    fields (the real logger stamps an ISO ``ts`` on it).

F43: every test runs ONE assertion body over the real
:class:`~kairix.core.search.logger.JsonlSearchLogger` (writing under
``tmp_path``) AND :class:`tests.fakes.FakeSearchLogger`.

Finding (reported): ``FakeSearchLogger(raises=...)`` propagates the
error, which no production SearchLogger does; pipeline tests use it to
exercise their own wrapping. The faithful shape is
``FakeSearchLogger(raises=..., contain_errors=True)``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from kairix.core.protocols import SearchLogger
from kairix.core.search.logger import JsonlSearchLogger
from tests.fakes import FakeSearchLogger

pytestmark = pytest.mark.contract

# (logger, read_back) — read_back returns every event the logger recorded.
LoggerUnderTest = tuple[SearchLogger, Callable[[], list[dict[str, Any]]]]
LoggerFactory = Callable[[Path, bool], LoggerUnderTest]


def _read_jsonl(*paths: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for path in paths:
        if path.is_file():
            events.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line)
    return events


def _real(tmp_path: Path, sink_fails: bool) -> LoggerUnderTest:
    """Real JSONL logger; a failing sink is a log path that is a directory."""
    search_path = tmp_path / "search.jsonl"
    query_path = tmp_path / "queries.jsonl"
    if sink_fails:
        search_path.mkdir()
        query_path.mkdir()
    logger = JsonlSearchLogger(search_log_path=search_path, query_log_path=query_path)
    return logger, lambda: _read_jsonl(search_path, query_path)


def _fake(_tmp_path: Path, sink_fails: bool) -> LoggerUnderTest:
    raises = IsADirectoryError("F68-search-log-sink-unwritable") if sink_fails else None
    logger = FakeSearchLogger(raises=raises, contain_errors=True)
    return logger, lambda: list(logger.events)


_IMPLS = [_real, _fake]


def _caller_fields(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip the logger-owned ``ts`` stamp so only caller-supplied fields remain."""
    return [{k: v for k, v in event.items() if k != "ts"} for event in events]


@pytest.mark.parametrize("factory", _IMPLS, ids=["real", "fake"])
def test_log_search_raises_inside_sink_is_contained(factory: LoggerFactory, tmp_path: Path) -> None:
    """A sink failure inside ``log_search`` must not escape — search must
    not break because logging broke — and no partial record is left.

    Sabotage proof: in ``kairix.core.search.logger.JsonlSearchLogger._append``
    narrow ``except (OSError, TypeError, ValueError)`` to
    ``except (TypeError, ValueError)``. Re-run: the real leg raises
    IsADirectoryError out of ``log_search``. Restored.
    """
    logger, read_back = factory(tmp_path, True)
    logger.log_search({"query_hash": "x", "intent": "semantic"})
    assert read_back() == []


@pytest.mark.parametrize("factory", _IMPLS, ids=["real", "fake"])
def test_log_query_raises_inside_sink_is_contained(factory: LoggerFactory, tmp_path: Path) -> None:
    """log_query sink failure is contained — same contract as log_search.

    Sabotage proof: as for log_search; the shared ``_append`` helper
    means a single mutation breaks both real legs.
    """
    logger, read_back = factory(tmp_path, True)
    logger.log_query({"query": "alpha", "query_hash": "x"})
    assert read_back() == []


@pytest.mark.parametrize("factory", _IMPLS, ids=["real", "fake"])
def test_log_search_returns_empty_when_event_is_empty_dict(factory: LoggerFactory, tmp_path: Path) -> None:
    """An empty event is recorded as-is (no validation pruning) — the
    Protocol promises capture, not validation. Observable: exactly one
    record, carrying no caller fields.

    Sabotage proof: in ``JsonlSearchLogger._append`` add
    ``if not event: return`` before the ``ts`` augmentation. Re-run:
    the real leg's ``== [{}]`` assertion fails (nothing written). Restored.
    """
    logger, read_back = factory(tmp_path, False)
    logger.log_search({})
    assert _caller_fields(read_back()) == [{}]


@pytest.mark.parametrize("factory", _IMPLS, ids=["real", "fake"])
def test_log_query_returns_empty_when_event_is_empty_dict(factory: LoggerFactory, tmp_path: Path) -> None:
    """Empty-event shape for log_query mirrors log_search.

    Sabotage proof: in ``JsonlSearchLogger.log_query`` change the body
    to ``return`` (drop the ``_append``). Re-run: the real leg's
    ``== [{}]`` assertion fails. Restored.
    """
    logger, read_back = factory(tmp_path, False)
    logger.log_query({})
    assert _caller_fields(read_back()) == [{}]
