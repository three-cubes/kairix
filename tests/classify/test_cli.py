"""
Tests for the classify CLI (kairix/classify/cli.py).

Tests CLI output format and argument parsing.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout

import pytest


def run_classify_cli(args: list[str]) -> tuple[str, str, int]:
    """Run classify CLI and return (stdout, stderr, exit_code)."""
    from kairix.core.classify.cli import main

    stdout_capture = io.StringIO()
    stderr_capture = io.StringIO()
    exit_code = 0

    try:
        with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
            main(args)
    except SystemExit as e:
        exit_code = e.code or 0

    return stdout_capture.getvalue(), stderr_capture.getvalue(), exit_code


@pytest.mark.unit
class TestClassifyCLIOutput:
    @pytest.mark.unit
    def test_procedural_rule_output_format(self):
        stdout, _, exit_code = run_classify_cli(
            [
                "Never write credentials to disk. Always fetch from Key Vault at runtime.",
                "--no-llm",
            ]
        )
        assert exit_code == 0
        parsed = json.loads(stdout.strip())
        assert parsed["type"] == "procedural-rule"
        assert "target_path" in parsed
        assert "confidence" in parsed
        assert "reason" in parsed

    @pytest.mark.unit
    def test_episodic_output_format(self):
        stdout, _, exit_code = run_classify_cli(["## 09:15\nFixed the RRF bug", "--no-llm"])
        assert exit_code == 0
        parsed = json.loads(stdout.strip())
        assert parsed["type"] == "episodic"

    @pytest.mark.unit
    def test_unknown_type_no_llm(self):
        stdout, _, exit_code = run_classify_cli(["Some generic statement with no clear classification.", "--no-llm"])
        assert exit_code == 0
        parsed = json.loads(stdout.strip())
        assert parsed["type"] == "unknown"
        assert parsed.get("needs_confirmation") is True

    @pytest.mark.unit
    def test_agent_flag(self):
        stdout, _, exit_code = run_classify_cli(["rule: never do bad things", "--agent", "builder", "--no-llm"])
        assert exit_code == 0
        parsed = json.loads(stdout.strip())
        assert "builder" in parsed["target_path"]

    @pytest.mark.unit
    def test_invalid_agent_exits_1(self):
        _, _stderr, exit_code = run_classify_cli(["some content", "--agent", "INVALID_AGENT", "--no-llm"])
        assert exit_code != 0

    @pytest.mark.unit
    def test_confidence_is_float(self):
        stdout, _, _exit_code = run_classify_cli(["decided: use Python 3.12", "--no-llm"])
        parsed = json.loads(stdout.strip())
        assert isinstance(parsed["confidence"], float)

    @pytest.mark.unit
    def test_target_path_is_string(self):
        stdout, _, _exit_code2 = run_classify_cli(["always use Key Vault for secrets", "--no-llm"])
        parsed = json.loads(stdout.strip())
        assert isinstance(parsed["target_path"], str)

    @pytest.mark.unit
    def test_needs_confirmation_only_when_low_confidence(self):
        stdout, _, _ = run_classify_cli(["## 14:22\nDid stuff", "--no-llm"])
        parsed = json.loads(stdout.strip())
        # Rule-based results have confidence 0.90 — no needs_confirmation
        assert "needs_confirmation" not in parsed or parsed.get("needs_confirmation") is False

    @pytest.mark.unit
    def test_semantic_decision_shape_agent(self):
        stdout, _, exit_code = run_classify_cli(["we chose to use FastAPI", "--agent", "shape", "--no-llm"])
        assert exit_code == 0
        parsed = json.loads(stdout.strip())
        assert parsed["type"] == "semantic-decision"
        assert "shape" in parsed["target_path"]
