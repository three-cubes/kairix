"""Confidence parsing strategies for the research agent.

The research agent asks an LLM to rate how well retrieved chunks answer
a query. Older code expected strict JSON output and silently fell back
to 0.0 when the LLM emitted prose. This module replaces that single-shot
approach with a Strategy chain:

  1. JsonModeConfidenceParser — strict json.loads of the response.
  2. RegexExtractConfidenceParser — pulls a confidence-shaped value out
     of prose (e.g. ``Confidence: 70%`` or ``confidence is 0.7``).
  3. ChainedConfidenceParser — composes parsers in order, logs a WARNING
     for each fallthrough so observability surfaces LLM non-compliance.

Production callers should use ``default_confidence_parser_chain()``.
"""

from __future__ import annotations

import json
import logging
import re

from kairix.agents.research.protocols import ConfidenceParseError, ConfidenceParser

logger = logging.getLogger(__name__)

# Regex that accepts the common shapes the LLM emits in prose.
# Named groups: ``value`` is the numeric portion, ``percent`` is set when
# the value is followed by a percent sign.
_CONFIDENCE_PATTERN = re.compile(
    r"""
    (?:["']?confidence["']?)       # 'confidence' optionally quoted
    \s*                             # optional whitespace
    (?:[:=]|\bis\b)                 # ':', '=', or the word 'is'
    \s*                             # optional whitespace
    (?P<value>-?\d+(?:\.\d+)?)      # the number (possibly negative / decimal)
    \s*
    (?P<percent>%)?                 # optional percent sign
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _clamp_unit_interval(value: float) -> float:
    """Clamp a float to the closed interval [0.0, 1.0]."""
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


class JsonModeConfidenceParser:
    """Parse confidence by interpreting the response as strict JSON.

    Reads the ``"confidence"`` top-level key. Raises
    :class:`ConfidenceParseError` when the response is not valid JSON,
    when the value is not a JSON object, or when the key is missing or
    not a number. The result is clamped to ``[0.0, 1.0]``.
    """

    def parse(self, response: str) -> float:
        try:
            parsed = json.loads(response)
        except json.JSONDecodeError as exc:
            raise ConfidenceParseError(f"response is not valid JSON: {exc.msg}") from exc

        if not isinstance(parsed, dict):
            raise ConfidenceParseError("JSON response is not an object")

        if "confidence" not in parsed:
            raise ConfidenceParseError("JSON response missing 'confidence' key")

        raw = parsed["confidence"]
        if not isinstance(raw, (int, float)) or isinstance(raw, bool):
            raise ConfidenceParseError("'confidence' value is not numeric")

        return _clamp_unit_interval(float(raw))


class RegexExtractConfidenceParser:
    """Parse confidence by extracting a confidence-shaped value from prose.

    Accepts shapes such as ``"confidence": 0.7``, ``confidence: 0.7``,
    ``confidence is 0.7``, ``confidence=0.7``, and ``Confidence: 70%``.
    Percent values are divided by 100. The result is clamped to
    ``[0.0, 1.0]``. Raises :class:`ConfidenceParseError` when no match.
    """

    def parse(self, response: str) -> float:
        match = _CONFIDENCE_PATTERN.search(response)
        if match is None:
            raise ConfidenceParseError("no confidence-shaped value found in response")

        value = float(match.group("value"))
        if match.group("percent"):
            value = value / 100.0
        return _clamp_unit_interval(value)


class ChainedConfidenceParser:
    """Try each parser in order; first non-raising parser wins.

    Logs a WARNING for every :class:`ConfidenceParseError` caught so that
    LLM non-compliance shows up in observability. If every parser raises,
    raises :class:`ConfidenceParseError` with the message
    ``"all parsers failed"``.
    """

    def __init__(self, parsers: list[ConfidenceParser]) -> None:
        self.parsers: list[ConfidenceParser] = list(parsers)

    def parse(self, response: str) -> float:
        snippet = response[:200]
        for parser in self.parsers:
            try:
                return parser.parse(response)
            except ConfidenceParseError as exc:
                logger.warning(
                    "ConfidenceParser %s failed: %s | response[:200]=%r",
                    type(parser).__name__,
                    exc,
                    snippet,
                )
        raise ConfidenceParseError("all parsers failed")


def default_confidence_parser_chain() -> ChainedConfidenceParser:
    """Return the production parser chain: JSON mode first, regex fallback."""
    return ChainedConfidenceParser([JsonModeConfidenceParser(), RegexExtractConfidenceParser()])
