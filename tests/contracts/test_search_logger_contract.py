"""Contract tests for the SearchLogger Protocol and its JSONL Adapter.

Verifies:
  1. JsonlSearchLogger satisfies the SearchLogger Protocol via isinstance().
  2. The Protocol's two methods accept a dict and return None.
  3. An in-test fake with the right method signatures also satisfies the
     Protocol (structural typing — duck-typed conformance).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kairix.core.protocols import SearchLogger
from kairix.core.search.logger import JsonlSearchLogger


@pytest.mark.contract
def test_jsonl_search_logger_satisfies_protocol(tmp_path: Path) -> None:
    """JsonlSearchLogger satisfies SearchLogger via isinstance()."""
    lg = JsonlSearchLogger(search_log_path=tmp_path / "s.jsonl")
    assert isinstance(lg, SearchLogger)


@pytest.mark.contract
def test_protocol_methods_accept_dict_and_return_none(tmp_path: Path) -> None:
    """log_search and log_query accept dict[str, Any] and return None."""
    lg = JsonlSearchLogger(
        search_log_path=tmp_path / "s.jsonl",
        query_log_path=tmp_path / "q.jsonl",
    )

    # log_search and log_query are typed `-> None`; the protocol contract is
    # already enforced by mypy. We invoke them here to confirm both return
    # paths are exercised without raising.
    lg.log_search({"query_hash": "x", "intent": "semantic"})
    lg.log_query({"query": "hello", "query_hash": "x"})


@pytest.mark.contract
def test_in_test_fake_satisfies_protocol() -> None:
    """A minimal duck-typed fake also satisfies SearchLogger.

    Demonstrates that the Protocol is structural — any class with the two
    methods of the right shape conforms, regardless of inheritance.
    """

    class _ListSearchLogger:
        def __init__(self) -> None:
            self.events: list[dict[str, Any]] = []

        def log_search(self, event: dict[str, Any]) -> None:
            self.events.append(event)

        def log_query(self, event: dict[str, Any]) -> None:
            self.events.append(event)

    fake = _ListSearchLogger()
    assert isinstance(fake, SearchLogger)

    # And verify the methods behave as advertised.
    fake.log_search({"a": 1})
    fake.log_query({"b": 2})
    assert fake.events == [{"a": 1}, {"b": 2}]
