"""Step definitions for recall_check.feature.

Adaptive recall quality gate. Uses in-memory SQLite databases to simulate
indexed document state and a tmp-path log file for the gate scenarios. No
monkeypatch, no external API calls. The degradation scenarios invoke the
real ``run_recall_gate`` against an injected ``RecallChecker`` whose
``check`` method returns a configured score — so the assertions test the
production gate logic, not the test's own arithmetic.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from pytest_bdd import given, parsers, then, when

from kairix.core.embed.recall_check import (
    DEFAULT_RECALL_QUERIES,
    RecallChecker,
    build_adaptive_queries,
    get_recall_queries,
    run_recall_gate,
)

pytestmark = pytest.mark.bdd

_state: dict = {}


@pytest.fixture(autouse=True)
def _recall_scenario_state(tmp_path: Path) -> None:
    """Fresh state and tmp_path at the start of each scenario."""
    _state.clear()
    _state["tmp_path"] = tmp_path


# ---------------------------------------------------------------------------
# Scenario: Adaptive queries are generated from indexed documents
# ---------------------------------------------------------------------------


@given("an index with titled documents")
def index_with_titled_documents() -> None:
    db = sqlite3.connect(":memory:")
    db.executescript(
        """
        CREATE TABLE documents (path TEXT, title TEXT, active INTEGER);
        INSERT INTO documents VALUES ('docs/architecture.md', 'architecture', 1);
        INSERT INTO documents VALUES ('docs/deploy-guide.md', 'deploy-guide', 1);
        INSERT INTO documents VALUES ('docs/testing.md', 'testing', 1);
        INSERT INTO documents VALUES ('docs/onboarding.md', 'onboarding', 1);
        """
    )
    db.commit()
    _state["db"] = db


@when("the recall check builds adaptive queries")
def step_build_adaptive_queries() -> None:
    _state["queries"] = build_adaptive_queries(_state["db"])


@then("at least 3 recall queries are generated")
def at_least_3_queries() -> None:
    assert len(_state["queries"]) >= 3


@then("each query has an id, query text, and expected fragment")
def each_query_has_fields() -> None:
    for qid, query, fragment in _state["queries"]:
        assert isinstance(qid, str) and qid.startswith("A"), f"unexpected id: {qid!r}"
        assert isinstance(query, str) and query, "query text empty"
        assert isinstance(fragment, str) and fragment, "fragment empty"


# ---------------------------------------------------------------------------
# Scenario: Default recall queries are used when no documents exist
# ---------------------------------------------------------------------------


@given("an empty search index")
def empty_search_index() -> None:
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE documents (path TEXT, title TEXT, active INTEGER)")
    db.commit()
    _state["db"] = db


@when("the recall check builds queries")
def build_queries() -> None:
    # cache_path=None bypasses the persistent canary cache so the BDD scenario
    # exercises the build path each run (otherwise the first scenario
    # populates ~/.cache/kairix/recall-canaries.json and subsequent
    # scenarios load from that cache).
    _state["queries"] = get_recall_queries(_state["db"], cache_path=None)


@then("the default recall queries are used")
def default_queries_used() -> None:
    assert _state["queries"] == list(DEFAULT_RECALL_QUERIES)


@then("at least 5 queries are returned")
def at_least_5_queries() -> None:
    assert len(_state["queries"]) >= 5


# ---------------------------------------------------------------------------
# Scenario: Degradation triggers alert (now exercises run_recall_gate end-to-end)
# ---------------------------------------------------------------------------


@given(parsers.parse("a previous recall log file recording a score of {score:f}"))
def previous_recall_log(score: float) -> None:
    log = _state["tmp_path"] / "recall-check.json"
    log.write_text(json.dumps([{"score": score}]))
    _state["log_path"] = log
    _state["previous_score"] = score


@given(parsers.parse("a recall checker configured to return a current score of {score:f}"))
def configured_checker(score: float) -> None:
    captured_score = score

    class _StaticChecker(RecallChecker):
        def check(self, **kwargs: object) -> dict[str, object]:
            return {
                "score": captured_score,
                "passed": round(captured_score * 5),
                "total": 5,
                "timestamp": 0,
                "detail": [],
            }

    _state["checker"] = _StaticChecker()
    _state["current_score"] = captured_score


@when("the operator runs the recall gate")
def operator_runs_gate() -> None:
    captured_alerts: list[str] = []
    passed, result = run_recall_gate(
        alert_callback=captured_alerts.append,
        checker=_state["checker"],
        log_path=_state["log_path"],
    )
    _state["passed"] = passed
    _state["result"] = result
    _state["alerts"] = captured_alerts


@then("the gate reports the run as failed")
def gate_failed() -> None:
    assert _state["passed"] is False, (
        f"expected failure but gate passed; previous={_state['previous_score']}, current={_state['current_score']}"
    )


@then("the gate reports the run as passed")
def gate_passed() -> None:
    assert _state["passed"] is True, (
        f"expected pass but gate failed; previous={_state['previous_score']}, "
        f"current={_state['current_score']}, alerts={_state['alerts']}"
    )


@then("the alert callback is invoked exactly once")
def alert_invoked_once() -> None:
    assert len(_state["alerts"]) == 1, f"expected 1 alert, got {len(_state['alerts'])}"


@then("the alert callback is not invoked")
def alert_not_invoked() -> None:
    assert _state["alerts"] == [], f"alert was invoked: {_state['alerts']}"


@then("the alert message names the previous and current scores")
def alert_message_names_scores() -> None:
    msg = _state["alerts"][0]
    prev_pct = f"{int(_state['previous_score'] * 100)}%"
    curr_pct = f"{int(_state['current_score'] * 100)}%"
    assert prev_pct in msg, f"previous {prev_pct} not in: {msg}"
    assert curr_pct in msg, f"current {curr_pct} not in: {msg}"
