"""Unit tests for the Scope enum."""

from __future__ import annotations

import pytest

from kairix.core.search.scope import Scope


@pytest.mark.unit
def test_parse_shared_returns_shared() -> None:
    assert Scope.parse("shared") is Scope.SHARED


@pytest.mark.unit
def test_parse_shared_agent_returns_shared_agent() -> None:
    assert Scope.parse("shared+agent") is Scope.SHARED_AGENT


@pytest.mark.unit
def test_parse_all_agents_returns_all_agents() -> None:
    assert Scope.parse("all-agents") is Scope.ALL_AGENTS


@pytest.mark.unit
def test_parse_everything_returns_everything() -> None:
    assert Scope.parse("everything") is Scope.EVERYTHING


@pytest.mark.unit
def test_parse_unknown_value_raises_with_offending_and_valid_listed() -> None:
    with pytest.raises(ValueError) as excinfo:
        Scope.parse("nope")
    message = str(excinfo.value)
    # Message names the offending value.
    assert "nope" in message
    # Message lists every valid value.
    for valid in ("shared", "agent", "shared+agent", "all-agents", "everything"):
        assert valid in message


@pytest.mark.unit
def test_parse_is_idempotent_on_enum_input() -> None:
    assert Scope.parse(Scope.SHARED) is Scope.SHARED
    assert Scope.parse(Scope.AGENT) is Scope.AGENT
    assert Scope.parse(Scope.SHARED_AGENT) is Scope.SHARED_AGENT
    assert Scope.parse(Scope.ALL_AGENTS) is Scope.ALL_AGENTS
    assert Scope.parse(Scope.EVERYTHING) is Scope.EVERYTHING


@pytest.mark.unit
def test_string_equality_preserved_for_all_members() -> None:
    """Backwards-compat invariant: every member equals its .value as a str."""
    assert Scope.SHARED == "shared"
    assert Scope.AGENT == "agent"
    assert Scope.SHARED_AGENT == "shared+agent"
    assert Scope.ALL_AGENTS == "all-agents"
    assert Scope.EVERYTHING == "everything"
    # And the universally-quantified form, to guard future additions.
    for member in Scope:
        assert member == member.value


@pytest.mark.unit
def test_enum_has_exactly_five_members() -> None:
    """Guards against accidental additions/removals."""
    assert len(list(Scope)) == 5
