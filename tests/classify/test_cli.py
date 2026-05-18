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


@pytest.mark.unit
class TestClassifyCLIStdinAndArgv:
    @pytest.mark.unit
    def test_reads_content_from_stdin(self, monkeypatch):
        """When no content arg and stdin not a tty, content is read from stdin."""
        import io

        from kairix.core.classify.cli import main

        monkeypatch.setattr("sys.stdin", io.StringIO("## 09:00\nFixed a bug today\n"))
        monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
        # Stub isatty since StringIO inherits but doesn't override predictably
        fake_stdin = io.StringIO("## 09:00\nFixed a bug today\n")
        fake_stdin.isatty = lambda: False  # type: ignore[method-assign]  # stubbing StringIO.isatty for the CLI to treat stdin as a pipe
        monkeypatch.setattr("sys.stdin", fake_stdin)

        stdout_capture = io.StringIO()
        with redirect_stdout(stdout_capture):
            main(["--no-llm"])
        parsed = json.loads(stdout_capture.getvalue().strip())
        assert "type" in parsed

    @pytest.mark.unit
    def test_exits_when_no_content_and_tty(self, monkeypatch):
        """If no content and stdin is a tty, prints error and exits 1."""
        import io

        from kairix.core.classify.cli import main

        fake_stdin = io.StringIO("")
        fake_stdin.isatty = lambda: True  # type: ignore[method-assign]  # stubbing StringIO.isatty so the CLI treats stdin as a TTY
        monkeypatch.setattr("sys.stdin", fake_stdin)

        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()
        with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
            with pytest.raises(SystemExit) as exc:
                main(["--no-llm"])
        assert exc.value.code == 1
        assert "no content provided" in stderr_capture.getvalue()

    @pytest.mark.unit
    def test_main_default_argv_strips_subcommand(self, monkeypatch):
        """When args=None, main reads sys.argv[2:] (strips 'kairix classify')."""
        import io

        from kairix.core.classify.cli import main

        monkeypatch.setattr(
            "sys.argv",
            ["kairix", "classify", "Never write credentials to disk.", "--no-llm"],
        )
        stdout_capture = io.StringIO()
        with redirect_stdout(stdout_capture):
            main()  # args=None -> reads sys.argv[2:]
        parsed = json.loads(stdout_capture.getvalue().strip())
        assert parsed["type"] == "procedural-rule"


@pytest.mark.unit
class TestClassifyCLIErrors:
    """CLI error paths driven through the public ``rule_classifier`` /
    ``llm_classifier`` DI kwargs on :func:`kairix.core.classify.cli.main`.

    The CLI lazy-imports ``classify_content`` and ``classify_with_llm`` from
    the kairix internals; tests inject fakes through the kwarg seam instead
    of monkey-patching those modules. The kwargs are the public production
    contract — see the docstring on ``main``.
    """

    @pytest.mark.unit
    def test_value_error_exits_one(self) -> None:
        """ValueError raised by the rule classifier surfaces as exit 1 with JSON error."""
        import io

        from kairix.core.classify import cli as cli_mod

        def _raise_value_error(content: str, *, agent: str) -> None:
            raise ValueError("bad agent state")

        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()
        with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
            with pytest.raises(SystemExit) as exc:
                cli_mod.main(["test content", "--no-llm"], rule_classifier=_raise_value_error)
        assert exc.value.code == 1
        err_json = json.loads(stderr_capture.getvalue().strip())
        assert "error" in err_json

    @pytest.mark.unit
    def test_unexpected_exception_exits_one(self) -> None:
        """Generic Exception surfaces as exit 1 with masked error."""
        import io

        from kairix.core.classify import cli as cli_mod

        def _raise_runtime(content: str, *, agent: str) -> None:
            raise RuntimeError("boom")

        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()
        with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
            with pytest.raises(SystemExit) as exc:
                cli_mod.main(["test content", "--no-llm"], rule_classifier=_raise_runtime)
        assert exc.value.code == 1
        err_json = json.loads(stderr_capture.getvalue().strip())
        assert "error" in err_json
        # Error message should not leak internal details
        assert "boom" not in err_json["error"]

    @pytest.mark.unit
    def test_llm_fallback_when_rule_unknown(self) -> None:
        """When rule classification returns 'unknown' and ``--no-llm`` is not set,
        the LLM classifier is invoked through the public kwarg seam."""
        import io
        from dataclasses import dataclass
        from typing import Any

        from kairix.core.classify import cli as cli_mod

        @dataclass
        class _FakeResult:
            type: str
            target_path: str
            confidence: float
            reason: str
            needs_confirmation: bool = False

        def _fake_llm(content: str, *, agent: str) -> Any:
            return _FakeResult(
                type="semantic-decision",
                target_path=f"04-Agent-Knowledge/{agent}/decisions.md",
                confidence=0.78,
                reason="llm fallback hit",
            )

        def _rule_unknown(content: str, *, agent: str) -> Any:
            return _FakeResult(
                type="unknown",
                target_path="",
                confidence=0.0,
                reason="no rule",
                needs_confirmation=True,
            )

        stdout_capture = io.StringIO()
        with redirect_stdout(stdout_capture):
            cli_mod.main(
                ["arbitrary content"],
                rule_classifier=_rule_unknown,
                llm_classifier=_fake_llm,
            )
        parsed = json.loads(stdout_capture.getvalue().strip())
        assert parsed["type"] == "semantic-decision"
        assert parsed["reason"] == "llm fallback hit"
