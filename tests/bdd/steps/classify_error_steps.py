"""Step definitions for the @error and LLM-fallback scenarios in classify.feature.

These scenarios drive ``kairix.core.classify.cli.main`` through its public
``rule_classifier`` / ``llm_classifier`` kwargs (F1-clean — no
``monkeypatch.setattr`` on kairix internals). The CLI's contract: on a
ValueError or generic Exception from the rule classifier it exits 1 with a
structured JSON error envelope on stderr; on rule-result type ``"unknown"``
with ``--no-llm`` unset it falls through to the LLM classifier.

Step phrases are deliberately distinct from ``classify_steps.py`` so the
pytest-bdd step registry never has to disambiguate between the two modules.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

from kairix.core.classify import cli as classify_cli

pytestmark = pytest.mark.bdd


@dataclass
class _FakeResult:
    """Classifier result-shape — mirrors the production dataclass for assertions."""

    type: str
    target_path: str
    confidence: float
    reason: str
    needs_confirmation: bool = False


@pytest.fixture
def _classify_err_state() -> dict[str, Any]:
    """Per-scenario state holder for content / agent / classifier overrides."""
    return {
        "content": "",
        "agent": "shared",
        "rule_classifier": None,
        "llm_classifier": None,
        "no_llm": False,
        "stdout": "",
        "stderr": "",
        "exit_code": None,
    }


@given(parsers.parse('classify CLI inputs content="{content}" agent="{agent}"'))
def _set_inputs(_classify_err_state: dict[str, Any], content: str, agent: str) -> None:
    _classify_err_state["content"] = content
    _classify_err_state["agent"] = agent


@given("the injected rule classifier raises ValueError")
def _rule_raises_value_error(_classify_err_state: dict[str, Any]) -> None:
    def _raises(content: str, *, agent: str) -> Any:
        raise ValueError("bad agent state")

    _classify_err_state["rule_classifier"] = _raises


@given(parsers.parse('the injected rule classifier raises RuntimeError carrying "{message}"'))
def _rule_raises_runtime(_classify_err_state: dict[str, Any], message: str) -> None:
    def _raises(content: str, *, agent: str) -> Any:
        raise RuntimeError(message)

    _classify_err_state["rule_classifier"] = _raises


@given(parsers.parse('the injected rule classifier returns type "{type_name}"'))
def _rule_returns_type(_classify_err_state: dict[str, Any], type_name: str) -> None:
    def _returns(content: str, *, agent: str) -> Any:
        return _FakeResult(
            type=type_name,
            target_path="",
            confidence=0.0,
            reason="rule-stub",
            needs_confirmation=(type_name == "unknown"),
        )

    _classify_err_state["rule_classifier"] = _returns


@given(parsers.parse('the injected LLM classifier returns type "{type_name}" with reason "{reason}"'))
def _llm_returns(_classify_err_state: dict[str, Any], type_name: str, reason: str) -> None:
    def _returns(content: str, *, agent: str) -> Any:
        return _FakeResult(
            type=type_name,
            target_path=f"04-Agent-Knowledge/{agent}/decisions.md",
            confidence=0.78,
            reason=reason,
        )

    _classify_err_state["llm_classifier"] = _returns


@when("the operator invokes the classify CLI with --no-llm")
def _run_no_llm(_classify_err_state: dict[str, Any]) -> None:
    _classify_err_state["no_llm"] = True
    _invoke_cli(_classify_err_state)


@when("the operator invokes the classify CLI without --no-llm")
def _run_with_llm_fallback(_classify_err_state: dict[str, Any]) -> None:
    _classify_err_state["no_llm"] = False
    _invoke_cli(_classify_err_state)


def _invoke_cli(state: dict[str, Any]) -> None:
    args = [state["content"], "--agent", state["agent"]]
    if state["no_llm"]:
        args.append("--no-llm")
    kwargs: dict[str, Any] = {}
    if state["rule_classifier"] is not None:
        kwargs["rule_classifier"] = state["rule_classifier"]
    if state["llm_classifier"] is not None:
        kwargs["llm_classifier"] = state["llm_classifier"]
    out, err = io.StringIO(), io.StringIO()
    code: int | None = 0
    try:
        with redirect_stdout(out), redirect_stderr(err):
            classify_cli.main(args, **kwargs)
    except SystemExit as exc:
        code = int(exc.code) if exc.code is not None else 0
    state["stdout"] = out.getvalue()
    state["stderr"] = err.getvalue()
    state["exit_code"] = code


@then("the classify CLI exits with code 1")
def _then_exit_one(_classify_err_state: dict[str, Any]) -> None:
    assert _classify_err_state["exit_code"] == 1


@then("the classify CLI stderr contains a structured JSON error envelope")
def _then_structured_error(_classify_err_state: dict[str, Any]) -> None:
    err = _classify_err_state["stderr"].strip()
    payload = json.loads(err)
    assert "error" in payload, f"expected 'error' key in JSON envelope; got {payload!r}"


@then(parsers.parse('the classify CLI stderr error envelope does NOT leak "{secret}"'))
def _then_no_leak(_classify_err_state: dict[str, Any], secret: str) -> None:
    err = _classify_err_state["stderr"]
    payload = json.loads(err.strip())
    assert secret not in payload.get("error", ""), f"error envelope leaked {secret!r}: {payload!r}"


@then(parsers.parse('the classify CLI stdout JSON has type "{type_name}"'))
def _then_stdout_type(_classify_err_state: dict[str, Any], type_name: str) -> None:
    payload = json.loads(_classify_err_state["stdout"].strip())
    assert payload["type"] == type_name


@then(parsers.parse('the classify CLI stdout JSON has reason "{reason}"'))
def _then_stdout_reason(_classify_err_state: dict[str, Any], reason: str) -> None:
    payload = json.loads(_classify_err_state["stdout"].strip())
    assert payload["reason"] == reason
