"""Unit tests for the research agent confidence parsing strategies."""

from __future__ import annotations

import json
import logging

import pytest

from kairix.agents.research.confidence import (
    ChainedConfidenceParser,
    JsonModeConfidenceParser,
    RegexExtractConfidenceParser,
    default_confidence_parser_chain,
)
from kairix.agents.research.protocols import ConfidenceParseError

# ---------------------------------------------------------------------------
# In-test fakes (no mocks, no monkeypatch)
# ---------------------------------------------------------------------------


class _RaisingParser:
    """Test fake: always raises ConfidenceParseError."""

    def __init__(self, message: str = "boom") -> None:
        self.message = message
        self.calls = 0

    def parse(self, response: str) -> float:
        self.calls += 1
        raise ConfidenceParseError(self.message)


class _ConstantParser:
    """Test fake: returns a fixed confidence value."""

    def __init__(self, value: float) -> None:
        self.value = value
        self.calls = 0

    def parse(self, response: str) -> float:
        self.calls += 1
        return self.value


# ---------------------------------------------------------------------------
# JsonModeConfidenceParser
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestJsonModeConfidenceParser:
    def test_valid_json_with_confidence_returns_float(self) -> None:
        parser = JsonModeConfidenceParser()
        response = json.dumps({"confidence": 0.85, "reasoning": "good coverage"})
        assert parser.parse(response) == pytest.approx(0.85)

    def test_json_without_confidence_key_raises(self) -> None:
        parser = JsonModeConfidenceParser()
        response = json.dumps({"reasoning": "no confidence here"})
        with pytest.raises(ConfidenceParseError):
            parser.parse(response)

    def test_non_json_prose_raises(self) -> None:
        parser = JsonModeConfidenceParser()
        response = "I am pretty confident: about 0.7 or so."
        with pytest.raises(ConfidenceParseError):
            parser.parse(response)


# ---------------------------------------------------------------------------
# RegexExtractConfidenceParser
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRegexExtractConfidenceParser:
    @pytest.mark.parametrize(
        ("response", "expected"),
        [
            ('"confidence": 0.7', 0.7),
            ('"confidence" : 0.7', 0.7),
            ("confidence: 0.7", 0.7),
            ("confidence is 0.7", 0.7),
            ("Confidence: 70%", 0.7),
            ("confidence=0.7", 0.7),
        ],
    )
    def test_each_shape_extracts_correct_value(self, response: str, expected: float) -> None:
        parser = RegexExtractConfidenceParser()
        assert parser.parse(response) == pytest.approx(expected)

    def test_percent_form_is_divided_by_one_hundred(self) -> None:
        parser = RegexExtractConfidenceParser()
        assert parser.parse("Confidence: 70%") == pytest.approx(0.7)

    def test_value_above_one_is_clamped(self) -> None:
        parser = RegexExtractConfidenceParser()
        assert parser.parse("confidence: 1.5") == pytest.approx(1.0)

    def test_negative_value_is_clamped_to_zero(self) -> None:
        parser = RegexExtractConfidenceParser()
        assert parser.parse("confidence: -0.4") == pytest.approx(0.0)

    def test_irrelevant_prose_raises(self) -> None:
        parser = RegexExtractConfidenceParser()
        with pytest.raises(ConfidenceParseError):
            parser.parse("The dog walked across the room without comment.")


# ---------------------------------------------------------------------------
# ChainedConfidenceParser
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestChainedConfidenceParser:
    def test_first_failure_then_success_returns_success_and_logs_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        failing = _RaisingParser(message="bad json")
        succeeding = _ConstantParser(value=0.5)
        chain = ChainedConfidenceParser([failing, succeeding])

        with caplog.at_level(logging.WARNING, logger="kairix.agents.research.confidence"):
            result = chain.parse("ignored response")

        assert result == pytest.approx(0.5)
        assert failing.calls == 1
        assert succeeding.calls == 1

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "_RaisingParser" in warnings[0].getMessage()

    def test_all_failing_raises_and_logs_each_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        first = _RaisingParser(message="first failure")
        second = _RaisingParser(message="second failure")
        chain = ChainedConfidenceParser([first, second])

        with caplog.at_level(logging.WARNING, logger="kairix.agents.research.confidence"):
            with pytest.raises(ConfidenceParseError) as excinfo:
                chain.parse("ignored response")

        assert "all parsers failed" in str(excinfo.value)
        assert first.calls == 1
        assert second.calls == 1

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 2

    def test_empty_chain_raises(self) -> None:
        chain = ChainedConfidenceParser([])
        with pytest.raises(ConfidenceParseError):
            chain.parse("anything")


# ---------------------------------------------------------------------------
# default_confidence_parser_chain factory
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDefaultConfidenceParserChain:
    def test_returns_chained_parser_with_json_first_then_regex(self) -> None:
        chain = default_confidence_parser_chain()

        assert isinstance(chain, ChainedConfidenceParser)
        assert len(chain.parsers) == 2
        assert isinstance(chain.parsers[0], JsonModeConfidenceParser)
        assert isinstance(chain.parsers[1], RegexExtractConfidenceParser)

    def test_default_chain_parses_strict_json(self) -> None:
        chain = default_confidence_parser_chain()
        assert chain.parse(json.dumps({"confidence": 0.42})) == pytest.approx(0.42)

    def test_default_chain_falls_back_to_regex(self) -> None:
        chain = default_confidence_parser_chain()
        assert chain.parse("My Confidence: 80% based on the sources") == pytest.approx(0.8)
