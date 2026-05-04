"""Protocols for the research agent's pluggable behaviours.

Currently:
  - ConfidenceParser: extracts a numeric confidence from an LLM response.

Each Protocol is @runtime_checkable so contract tests can verify
conformance via isinstance().
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ConfidenceParser(Protocol):
    """Extracts a confidence score (0.0-1.0) from an LLM response string.

    Implementations may raise ConfidenceParseError to signal that the
    response does not contain a parseable confidence value (so the next
    parser in a chain can try). They MUST NOT silently return 0.0 to
    signal failure — that is what the bug we are fixing did.
    """

    def parse(self, response: str) -> float: ...


class ConfidenceParseError(ValueError):
    """Raised by a ConfidenceParser when the response is not parseable.

    Used to signal "try the next strategy in the chain" rather than
    "the document genuinely has 0.0 confidence."
    """
