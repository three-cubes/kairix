"""Integration tests for ``kairix classify`` end-to-end via the public CLI surface.

Drives :func:`kairix.core.classify.cli.main` with real arguments + the
public ``rule_classifier`` / ``llm_classifier`` DI kwargs. The CLI's
contract — JSON-on-stdout on success, structured-JSON-on-stderr on
failure, exit code 1 for errors — is what operators rely on when chaining
``kairix classify`` into shell pipelines or systemd-managed jobs.

Tests sit above :mod:`tests.classify.test_cli` (unit) and below
:mod:`tests.bdd.test_classify` (operator language). Together the three
layers pin the contract from three angles: unit proves the function
shape, integration proves the I/O wiring, BDD proves the operator-visible
semantics.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from typing import Any

import pytest

from kairix.core.classify import cli as classify_cli

pytestmark = pytest.mark.integration


@dataclass
class _ResultShape:
    type: str
    target_path: str
    confidence: float
    reason: str
    needs_confirmation: bool = False


def _invoke(args: list[str], **kwargs: Any) -> tuple[str, str, int]:
    """Run the CLI and capture (stdout, stderr, exit_code)."""
    out, err = io.StringIO(), io.StringIO()
    code: int = 0
    try:
        with redirect_stdout(out), redirect_stderr(err):
            classify_cli.main(args, **kwargs)
    except SystemExit as exc:
        code = int(exc.code) if exc.code is not None else 0
    return out.getvalue(), err.getvalue(), code


@pytest.mark.integration
def test_cli_writes_structured_json_on_success() -> None:
    """A rule-classifier hit emits a JSON object on stdout with the contract keys."""

    def _rule(content: str, *, agent: str) -> Any:
        return _ResultShape(
            type="procedural-rule",
            target_path=f"04-Agent-Knowledge/{agent}/patterns.md",
            confidence=0.92,
            reason="rule-match",
        )

    stdout, _stderr, code = _invoke(
        ["a content payload", "--agent", "builder", "--no-llm"],
        rule_classifier=_rule,
    )

    assert code == 0
    payload = json.loads(stdout.strip())
    assert payload == {
        "type": "procedural-rule",
        "target_path": "04-Agent-Knowledge/builder/patterns.md",
        "confidence": 0.92,
        "reason": "rule-match",
    }


@pytest.mark.integration
def test_cli_emits_needs_confirmation_when_classifier_flags_it() -> None:
    """When the classifier sets ``needs_confirmation=True`` the CLI surfaces it."""

    def _rule(content: str, *, agent: str) -> Any:
        return _ResultShape(
            type="ambiguous",
            target_path=f"04-Agent-Knowledge/{agent}/inbox.md",
            confidence=0.55,
            reason="needs human review",
            needs_confirmation=True,
        )

    stdout, _stderr, code = _invoke(
        ["something", "--agent", "builder", "--no-llm"],
        rule_classifier=_rule,
    )

    assert code == 0
    payload = json.loads(stdout.strip())
    assert payload.get("needs_confirmation") is True


@pytest.mark.integration
def test_cli_falls_through_to_llm_when_rule_returns_unknown_and_llm_enabled() -> None:
    """Rule ``unknown`` + ``--no-llm`` NOT set → LLM classifier is consulted."""
    llm_calls: list[tuple[str, str]] = []

    def _rule(content: str, *, agent: str) -> Any:
        return _ResultShape(
            type="unknown",
            target_path="",
            confidence=0.0,
            reason="no rule",
            needs_confirmation=True,
        )

    def _llm(content: str, *, agent: str) -> Any:
        llm_calls.append((content, agent))
        return _ResultShape(
            type="semantic-decision",
            target_path=f"04-Agent-Knowledge/{agent}/decisions.md",
            confidence=0.81,
            reason="llm hit",
        )

    stdout, _stderr, code = _invoke(
        ["content for llm", "--agent", "builder"],
        rule_classifier=_rule,
        llm_classifier=_llm,
    )

    assert code == 0
    assert llm_calls == [("content for llm", "builder")], "LLM must be invoked exactly once"
    payload = json.loads(stdout.strip())
    assert payload["type"] == "semantic-decision"
    assert payload["reason"] == "llm hit"


@pytest.mark.integration
def test_cli_skips_llm_when_no_llm_flag_set() -> None:
    """``--no-llm`` makes the rule ``unknown`` result final — LLM never runs."""
    llm_calls: list[Any] = []

    def _rule(content: str, *, agent: str) -> Any:
        return _ResultShape(type="unknown", target_path="", confidence=0.0, reason="no rule")

    def _llm(content: str, *, agent: str) -> Any:
        llm_calls.append((content, agent))
        return _ResultShape(type="semantic-decision", target_path="x", confidence=1.0, reason="should not run")

    stdout, _stderr, code = _invoke(
        ["content", "--agent", "builder", "--no-llm"],
        rule_classifier=_rule,
        llm_classifier=_llm,
    )

    assert code == 0
    assert llm_calls == [], "LLM must NOT be invoked when --no-llm is set"
    payload = json.loads(stdout.strip())
    assert payload["type"] == "unknown"


@pytest.mark.integration
def test_cli_invalid_agent_exits_1_before_calling_classifier() -> None:
    """An agent name outside VALID_AGENTS short-circuits before classifier runs."""
    classifier_calls: list[Any] = []

    def _rule(content: str, *, agent: str) -> Any:
        classifier_calls.append((content, agent))
        return _ResultShape(type="procedural-rule", target_path="x", confidence=1.0, reason="should not run")

    _stdout, stderr, code = _invoke(
        ["x", "--agent", "not-a-real-agent", "--no-llm"],
        rule_classifier=_rule,
    )

    assert code == 1
    assert classifier_calls == [], "classifier must not run for an invalid agent"
    assert "not-a-real-agent" in stderr
