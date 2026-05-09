"""Step definitions for search_logging.feature.

Wires a SearchPipeline (canonical fakes from tests/fakes.py) to a real
JsonlSearchLogger writing under tmp_path. No @patch, no monkeypatch.

Per-scenario state is held on a fixture (`logging_ctx`) so scenarios
remain isolated.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pytest_bdd import given, parsers, then, when

from kairix.core.search.backends import BM25SearchBackend, VectorSearchBackend
from kairix.core.search.config import RetrievalConfig
from kairix.core.search.fusion import RRFFusion
from kairix.core.search.intent import QueryIntent
from kairix.core.search.logger import JsonlSearchLogger
from kairix.core.search.pipeline import SearchPipeline, SearchResult
from kairix.core.search.scope import Scope
from tests.fakes import (
    FakeClassifier,
    FakeDocumentRepository,
    FakeEmbeddingService,
    FakeGraphRepository,
    FakeVectorRepository,
)


@dataclass
class _LoggingScenarioCtx:
    """Per-scenario state."""

    log_path: Path | None = None
    pipeline: SearchPipeline | None = None
    last_result: SearchResult | None = None
    last_exception: Exception | None = None
    results: list[SearchResult] = field(default_factory=list)


@pytest.fixture
def logging_ctx() -> _LoggingScenarioCtx:
    return _LoggingScenarioCtx()


def _build_pipeline(log_path: Path) -> SearchPipeline:
    docs = [
        {
            "path": "doc1.md",
            "collection": "shared-knowledge",
            "title": "Hello",
            "content": "hello world",
        },
    ]
    return SearchPipeline(
        classifier=FakeClassifier(intent=QueryIntent.SEMANTIC),
        bm25=BM25SearchBackend(FakeDocumentRepository(documents=docs)),
        vector=VectorSearchBackend(FakeEmbeddingService(), FakeVectorRepository()),
        graph=FakeGraphRepository(available=False),
        fusion=RRFFusion(k=60),
        boosts=[],
        logger=JsonlSearchLogger(search_log_path=log_path),
        config=RetrievalConfig.defaults(),
    )


# ---------------------------------------------------------------------------
# Background
# ---------------------------------------------------------------------------


@given("a kairix search pipeline wired to a JsonlSearchLogger writing to a temporary search log path")
def _build_logging_pipeline(
    logging_ctx: _LoggingScenarioCtx,
    tmp_path: Path,
) -> None:
    logging_ctx.log_path = tmp_path / "logs" / "search.jsonl"
    logging_ctx.pipeline = _build_pipeline(logging_ctx.log_path)


# ---------------------------------------------------------------------------
# Failure-mode setup: point search_log_path at a directory so writes fail.
# ---------------------------------------------------------------------------


@given("the search log path is unwritable")
def _unwritable_log_path(
    logging_ctx: _LoggingScenarioCtx,
    tmp_path: Path,
) -> None:
    # tmp_path itself is a directory — opening it for append raises
    # IsADirectoryError on every write, exercising the never-raise contract.
    logging_ctx.log_path = tmp_path
    logging_ctx.pipeline = _build_pipeline(tmp_path)


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


@when(
    parsers.re(
        r'the agent "(?P<agent>[^"]+)" runs a search for "(?P<query>[^"]+)" '
        r'with scope "(?P<scope>[^"]+)"'
    )
)
def _run_search(
    logging_ctx: _LoggingScenarioCtx,
    agent: str,
    query: str,
    scope: str,
) -> None:
    pipeline = logging_ctx.pipeline
    assert pipeline is not None, "pipeline must be built in Background"
    parsed_scope = Scope.parse(scope)

    logging_ctx.last_exception = None
    try:
        result = pipeline.search(query, agent=agent, scope=parsed_scope)
        logging_ctx.last_result = result
        logging_ctx.results.append(result)
    except Exception as exc:
        # SearchPipeline.search documents "Never raises". Capture only
        # programming-error-shaped exceptions so KeyboardInterrupt /
        # SystemExit still propagate and can stop the test runner.
        logging_ctx.last_exception = exc


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------


def _read_events(path: Path) -> list[dict[str, object]]:
    if not path.exists() or not path.is_file():
        return []
    return [json.loads(raw) for raw in path.read_text(encoding="utf-8").splitlines() if raw.strip()]


@then(parsers.parse("the search log contains exactly {count:d} JSONL line"))
@then(parsers.parse("the search log contains exactly {count:d} JSONL lines"))
def _assert_line_count(logging_ctx: _LoggingScenarioCtx, count: int) -> None:
    assert logging_ctx.log_path is not None
    rows = _read_events(logging_ctx.log_path)
    assert len(rows) == count, f"expected {count} JSONL lines, got {len(rows)}"


@then(
    parsers.re(
        r'the most recent search log event has field "(?P<field>[^"]+)" '
        r'equal to "(?P<value>[^"]+)"'
    )
)
def _assert_field_equals(
    logging_ctx: _LoggingScenarioCtx,
    field: str,
    value: str,
) -> None:
    assert logging_ctx.log_path is not None
    rows = _read_events(logging_ctx.log_path)
    assert rows, "no search log events to assert against"
    actual = rows[-1].get(field)
    assert actual == value, f"expected {field}={value!r}, got {actual!r}"


@then(parsers.re(r'the most recent search log event has a field "(?P<field>[^"]+)"'))
def _assert_field_present(
    logging_ctx: _LoggingScenarioCtx,
    field: str,
) -> None:
    assert logging_ctx.log_path is not None
    rows = _read_events(logging_ctx.log_path)
    assert rows, "no search log events to assert against"
    assert field in rows[-1], f"expected field {field!r} in event, got keys {list(rows[-1])}"


@then("the search call returned a SearchResult without raising")
def _assert_no_exception(logging_ctx: _LoggingScenarioCtx) -> None:
    assert logging_ctx.last_exception is None, f"search raised: {logging_ctx.last_exception!r}"
    assert isinstance(logging_ctx.last_result, SearchResult), (
        f"expected SearchResult, got {type(logging_ctx.last_result)!r}"
    )
