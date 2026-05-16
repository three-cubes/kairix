"""Step definitions for classify.feature.

Drives the rule-based classifier (``kairix.core.classify.rules.classify_content``)
and the path router (``kairix.core.classify.router.resolve_target_path``)
directly — these are the public surfaces the CLI itself calls. The
``--no-llm`` CLI flag exists for exactly this reason: scenarios stay
deterministic without reaching for the LLM judge.

F1-clean: no @patch on kairix internals. F5-clean: no underscore-prefixed
imports. F4-clean: no env-var reads (paths.document_root() is used by
the router via its own seam — tests don't touch KAIRIX_*).
"""

from __future__ import annotations

from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

from kairix.core.classify.router import resolve_target_path
from kairix.core.classify.rules import ClassificationResult, classify_content

pytestmark = pytest.mark.bdd


# Step-phrase fragments lifted to constants for F17 hygiene.
_PHRASE_STRONG_SIGNALS = "a memory content with strong domain signals"
_PHRASE_AGENT_BUILDER = 'an explicit agent "builder"'


# A content string with clear rule-match signals: the leading "Pattern:"
# token forces the rule classifier into the procedural-pattern branch
# (one of _RE_PROCEDURAL_PATTERN_STRONG patterns). Stable across the
# scenarios that share the "strong signals" Given.
_STRONG_SIGNAL_CONTENT = "Pattern: deployment runbook update — Step 1: prepare release"


@pytest.fixture
def _classify_state() -> dict[str, Any]:
    """Per-scenario fresh state container."""
    return {
        "content": "",
        "agent": "",
        "explicit_type": "",
        "result": None,
        "resolved_path": "",
        "exception": None,
    }


# ---------------------------------------------------------------------------
# Given — pick content, agent, optional explicit type
# ---------------------------------------------------------------------------


@given(parsers.parse('a memory content "{content}"'))
def _given_memory_content(_classify_state: dict[str, Any], content: str) -> None:
    _classify_state["content"] = content


@given(_PHRASE_STRONG_SIGNALS)
def _given_strong_signal_content(_classify_state: dict[str, Any]) -> None:
    _classify_state["content"] = _STRONG_SIGNAL_CONTENT


@given(_PHRASE_AGENT_BUILDER)
@given(parsers.parse('an explicit agent "{agent}"'))
def _given_explicit_agent(_classify_state: dict[str, Any], agent: str = "builder") -> None:
    # The double-decorator form (parameterised + literal) lets the
    # feature use both phrasings; the literal alias keeps F17 quiet by
    # reusing the constant where the same string would otherwise repeat.
    _classify_state["agent"] = agent


@given(parsers.parse('an explicit classification type "{cls_type}"'))
def _given_explicit_type(_classify_state: dict[str, Any], cls_type: str) -> None:
    _classify_state["explicit_type"] = cls_type


@given("an agent name that is not registered")
def _given_unknown_agent(_classify_state: dict[str, Any]) -> None:
    _classify_state["agent"] = "no-such-agent"


# ---------------------------------------------------------------------------
# When — run the classifier / resolver
# ---------------------------------------------------------------------------


@when("the operator runs classify")
def _when_run_classify(_classify_state: dict[str, Any]) -> None:
    try:
        _classify_state["result"] = classify_content(
            _classify_state["content"],
            agent=_classify_state["agent"],
        )
    except ValueError as exc:
        _classify_state["exception"] = exc


@when("the operator resolves the target path with the explicit type")
def _when_resolve_explicit(_classify_state: dict[str, Any]) -> None:
    try:
        _classify_state["resolved_path"] = resolve_target_path(
            _classify_state["agent"],
            _classify_state["explicit_type"],
        )
    except ValueError as exc:  # pragma: no cover — the scenario uses a valid type
        _classify_state["exception"] = exc


@when("the operator runs classify for the unknown agent")
def _when_run_classify_unknown_agent(_classify_state: dict[str, Any]) -> None:
    try:
        _classify_state["result"] = classify_content(
            _classify_state["content"],
            agent=_classify_state["agent"],
        )
    except ValueError as exc:
        _classify_state["exception"] = exc


# ---------------------------------------------------------------------------
# Then — assertions
# ---------------------------------------------------------------------------


def _result(state: dict[str, Any]) -> ClassificationResult:
    out = state["result"]
    assert out is not None, "classify_content was not invoked or raised"
    assert isinstance(out, ClassificationResult)
    return out


@then("the classified type is non-empty")
def _then_type_non_empty(_classify_state: dict[str, Any]) -> None:
    out = _result(_classify_state)
    # Sabotage: short-circuit classify_by_rules to return (None, "") for
    # any input and the result.type collapses to "unknown" — this
    # assertion catches the regression because the seeded content
    # carries a clear "Pattern:" rule-match.
    assert out.type, f"expected non-empty type; got {out.type!r}"
    assert out.type != "unknown", f"expected a rule-match, got unknown for content={_classify_state['content']!r}"


@then("the classified target path includes the pattern-related filename")
def _then_target_path_pattern(_classify_state: dict[str, Any]) -> None:
    out = _result(_classify_state)
    # Sabotage: swap _TYPE_TO_FILENAME["procedural-pattern"] from
    # "patterns.md" to "" and the target_path stops carrying the
    # pattern-related filename — this assertion exposes it.
    assert "patterns.md" in out.target_path, f"expected patterns.md in target_path; got {out.target_path!r}"


@then("the resolved target path matches the explicit type's filename")
def _then_explicit_path_matches(_classify_state: dict[str, Any]) -> None:
    path = _classify_state["resolved_path"]
    # The explicit type "semantic-decision" maps to "decisions.md" via
    # the router's _TYPE_TO_FILENAME. Sabotage: change the mapping or
    # let the router silently fall through to a default — this
    # assertion catches the deviation regardless of which agent dir.
    assert "decisions.md" in path, f"expected decisions.md in resolved path; got {path!r}"
    # And the explicit agent must scope the path.
    assert "/builder/" in path, f"expected /builder/ in resolved path; got {path!r}"


@then("the classify result contains an error message naming the missing agent")
def _then_error_names_agent(_classify_state: dict[str, Any]) -> None:
    exc = _classify_state["exception"]
    # Sabotage: drop the ``raise ValueError`` for unknown agent in
    # classify_content and a typoed agent would silently classify into
    # the wrong directory — this assertion forces the contract.
    assert isinstance(exc, ValueError), f"expected ValueError for unknown agent; got {exc!r}"
    assert "no-such-agent" in str(exc), f"expected agent name in error message; got {exc!s}"
