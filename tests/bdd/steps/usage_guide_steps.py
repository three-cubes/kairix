"""Step definitions for usage_guide.feature.

Drives ``kairix.use_cases.usage_guide.run_usage_guide`` through an
injected ``UsageGuideDeps.resolve_guide_fn`` that returns a per-scenario
markdown fixture written under ``tmp_path``. This is the documented
seam — production callers leave ``deps=None`` and the default factory
wires the real resolver (F1/F6-clean).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

from kairix.use_cases.usage_guide import (
    UsageGuideDeps,
    UsageGuideOutput,
    run_usage_guide,
)

pytestmark = pytest.mark.bdd


# Step-phrase fragments lifted to constants where the same literal would
# otherwise repeat ≥3 times in this module (F17).
_PHRASE_GUIDE_FIXTURE = "a usage guide fixture with multiple sections"
_PHRASE_REQUEST_WITH_TOPIC = "the agent requests the usage guide with that topic"


# A multi-section guide fixture: one section the "search" topic should
# hit, one capabilities-table section, and one that won't be hit by
# either the search keyword or any other test query — so the "unknown
# topic" scenario gets a clean fallback signature.
_GUIDE_TEXT = """# Kairix Agent Usage Guide

Welcome to the agent usage guide.

## Search
How to search the document store with the search subcommand.
Default scope covers shared and per-agent collections.

## Budget
Token budget controls cost. Default budget is 3000 tokens.

## Capabilities — which surface to use
- search: free-text retrieval
- brief: synthesised session briefing
- entity: graph lookup
- timeline: temporal queries

## Troubleshooting
Debug tips for common issues.
"""


@pytest.fixture
def _usage_guide_state(tmp_path: Path) -> dict[str, Any]:
    """Per-scenario fresh state container."""
    return {
        "guide_path": tmp_path / "agent-usage-guide.md",
        "topic": "",
        "result": None,
        "full_guide_text": "",
    }


# ---------------------------------------------------------------------------
# Given — seed the guide fixture / pick the topic
# ---------------------------------------------------------------------------


@given(_PHRASE_GUIDE_FIXTURE)
def _given_guide_fixture(_usage_guide_state: dict[str, Any]) -> None:
    path: Path = _usage_guide_state["guide_path"]
    path.write_text(_GUIDE_TEXT, encoding="utf-8")
    _usage_guide_state["full_guide_text"] = _GUIDE_TEXT


@given(parsers.parse('a known guide topic "{topic}"'))
def _given_known_topic(_usage_guide_state: dict[str, Any], topic: str) -> None:
    _usage_guide_state["topic"] = topic


@given("a guide topic that does not exist")
def _given_unknown_topic(_usage_guide_state: dict[str, Any]) -> None:
    # "zzzzzz-no-such-topic" appears neither in any heading nor on any
    # line of the fixture, forcing the first-2000-chars fallback path.
    _usage_guide_state["topic"] = "zzzzzz-no-such-topic"


# ---------------------------------------------------------------------------
# When — invoke run_usage_guide
# ---------------------------------------------------------------------------


@when("the agent requests the usage guide with no topic")
def _when_request_full(_usage_guide_state: dict[str, Any]) -> None:
    fixture_path: Path = _usage_guide_state["guide_path"]
    deps = UsageGuideDeps(resolve_guide_fn=lambda _override: fixture_path)
    _usage_guide_state["result"] = run_usage_guide("", deps=deps)


@when(_PHRASE_REQUEST_WITH_TOPIC)
def _when_request_with_topic(_usage_guide_state: dict[str, Any]) -> None:
    fixture_path: Path = _usage_guide_state["guide_path"]
    deps = UsageGuideDeps(resolve_guide_fn=lambda _override: fixture_path)
    _usage_guide_state["result"] = run_usage_guide(_usage_guide_state["topic"], deps=deps)


# ---------------------------------------------------------------------------
# Then — assertions on the response
# ---------------------------------------------------------------------------


def _result(state: dict[str, Any]) -> UsageGuideOutput:
    out = state["result"]
    assert out is not None, "run_usage_guide was not invoked"
    assert isinstance(out, UsageGuideOutput)
    return out


@then("the response contains the full guide text")
def _then_full_guide(_usage_guide_state: dict[str, Any]) -> None:
    out = _result(_usage_guide_state)
    # Sabotage: return only a slice instead of the full text when
    # topic == "" and this assertion catches the regression (the full
    # fixture body is shorter than ~1KB so byte-equality is feasible).
    assert out.content == _usage_guide_state["full_guide_text"], (
        f"expected full guide body; got first 80 chars={out.content[:80]!r}"
    )
    assert out.error == ""


@then("the response includes a capabilities-section reference")
def _then_capabilities_reference(_usage_guide_state: dict[str, Any]) -> None:
    out = _result(_usage_guide_state)
    # Sabotage: drop the "## Capabilities" heading from the fixture
    # render path (e.g. an over-eager filter) and this assertion fails.
    assert "Capabilities" in out.content, f"expected capabilities section in content; got {out.content!r}"


@then("the response is shorter than the full guide")
def _then_shorter_than_full(_usage_guide_state: dict[str, Any]) -> None:
    out = _result(_usage_guide_state)
    # Sabotage: have run_usage_guide ignore the topic filter and return
    # the full guide regardless — content would equal the full text
    # and this length check trips.
    full_length = len(_usage_guide_state["full_guide_text"])
    assert 0 < len(out.content) < full_length, (
        f"expected 0 < len(content)={len(out.content)} < full_length={full_length}"
    )


@then("the response mentions the topic name")
def _then_mentions_topic(_usage_guide_state: dict[str, Any]) -> None:
    out = _result(_usage_guide_state)
    topic = _usage_guide_state["topic"]
    # Sabotage: replace the heading-match with a hard-coded constant
    # section ("## Search" always) and the assertion still holds for
    # "search" but breaks for any other topic — exposed by future
    # parametrisation. For now the topic IS "search"; the case-
    # insensitive check pins the heading-match contract.
    assert topic.lower() in out.content.lower(), f"expected {topic!r} in content; got {out.content[:200]!r}"


@then("the response contains the fallback orientation slice")
def _then_fallback_slice(_usage_guide_state: dict[str, Any]) -> None:
    out = _result(_usage_guide_state)
    # When no heading/keyword matches, extract_topic_sections returns
    # the first 2000 chars of the guide. The fixture is shorter than
    # 2000 chars so the fallback returns the entire fixture body.
    # Sabotage: collapse the fallback to ``""`` (e.g. early-return on
    # zero matches) and the agent loses the orientation affordance —
    # this assertion catches the silent regression.
    assert out.content, f"expected non-empty fallback content; got {out.content!r}"
    assert "Kairix Agent Usage Guide" in out.content


@then("the fallback slice references at least one valid topic")
def _then_fallback_lists_topics(_usage_guide_state: dict[str, Any]) -> None:
    out = _result(_usage_guide_state)
    # Sabotage: the fallback path currently returns ``full_text[:2000]``
    # which embeds the fixture's "## Search" / "## Budget" / "## Capabilities"
    # headings. If a future refactor switches the fallback to a tagline
    # only (e.g. "no match"), the agent loses the topic discovery
    # affordance — this assertion guards that contract.
    valid_topics = ["Search", "Budget", "Capabilities", "Troubleshooting"]
    found = [t for t in valid_topics if t in out.content]
    assert found, f"expected ≥1 valid topic heading in fallback content; got content={out.content!r}"
