"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`ConfidenceParser`.

Single Protocol method ``parse(response)``. The Protocol docstring is
explicit: implementations MUST raise :class:`ConfidenceParseError` for
unparseable input, NOT silently return ``0.0`` (which is the bug this
Protocol was created to fix).

ONE body runs over every shipped parser — :class:`JsonModeConfidenceParser`,
:class:`RegexExtractConfidenceParser`, the production
``default_confidence_parser_chain()`` — AND the canonical
:class:`tests.fakes.FakeConfidenceParser` (F43 behavioural parity). Each
case carries the parser's own operator-facing message so the assertion
still pins which failure the parser reports.

Each test carries a "Sabotage proof:" comment describing the mutation
that proves the assertion has teeth.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from kairix.agents.research.confidence import (
    JsonModeConfidenceParser,
    RegexExtractConfidenceParser,
    default_confidence_parser_chain,
)
from kairix.agents.research.protocols import ConfidenceParseError, ConfidenceParser
from tests.fakes import FakeConfidenceParser

pytestmark = pytest.mark.contract

# (name, factory, the parser's failure message)
_IMPLEMENTATIONS: list[tuple[str, Callable[[], ConfidenceParser], str]] = [
    ("json_mode", JsonModeConfidenceParser, "not valid JSON"),
    ("regex", RegexExtractConfidenceParser, "no confidence-shaped value"),
    ("chain", default_confidence_parser_chain, "all parsers failed"),
    ("fake", FakeConfidenceParser, "no scripted confidence"),
]
_IDS = [impl[0] for impl in _IMPLEMENTATIONS]


@pytest.mark.parametrize("name,factory,message", _IMPLEMENTATIONS, ids=_IDS)
def test_parse_raises_on_unparseable_response(name: str, factory: Callable[[], ConfidenceParser], message: str) -> None:
    """A prose response with no confidence value MUST raise
    :class:`ConfidenceParseError` — silently returning 0.0 was the
    original bug, and silent fallback would mask LLM non-compliance.

    Sabotage proof (executed): in :meth:`RegexExtractConfidenceParser.parse`
    change ``raise ConfidenceParseError(...)`` to ``return 0.0``. Re-run:
    the ``regex`` AND ``chain`` cases fail (the chain's regex fallback now
    masks the failure). Restored. Same shape for the JSON parser's
    JSONDecodeError branch.
    """
    parser = factory()
    with pytest.raises(ConfidenceParseError, match=message):
        parser.parse("The agent responded with prose but no number.")
