"""Contract tests: ConfidenceParser protocol conformance.

ONE body per invariant, run over every shipped parser (JSON mode, regex
extract, the production chain) AND the canonical
:class:`tests.fakes.FakeConfidenceParser` (F43 behavioural parity).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from kairix.agents.research.confidence import (
    ChainedConfidenceParser,
    JsonModeConfidenceParser,
    RegexExtractConfidenceParser,
    default_confidence_parser_chain,
)
from kairix.agents.research.protocols import ConfidenceParser
from tests.fakes import FakeConfidenceParser

pytestmark = pytest.mark.contract

_COMPLIANT = '{"confidence": 0.7}'
_OVER_RANGE = '{"confidence": 1.7}'

_IMPLEMENTATIONS: list[tuple[str, Callable[[], ConfidenceParser]]] = [
    ("json_mode", JsonModeConfidenceParser),
    ("regex", RegexExtractConfidenceParser),
    ("chained", lambda: ChainedConfidenceParser([JsonModeConfidenceParser()])),
    ("default_chain", default_confidence_parser_chain),
    ("fake", lambda: FakeConfidenceParser({_COMPLIANT: 0.7, _OVER_RANGE: 1.7})),
]
_IDS = [impl[0] for impl in _IMPLEMENTATIONS]


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS, ids=_IDS)
def test_parser_satisfies_protocol(name: str, factory: Callable[[], ConfidenceParser]) -> None:
    assert isinstance(factory(), ConfidenceParser), name


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS, ids=_IDS)
def test_parser_returns_clamped_confidence_for_compliant_response(
    name: str, factory: Callable[[], ConfidenceParser]
) -> None:
    """A compliant JSON response parses to its value; an out-of-range
    value is clamped into ``[0.0, 1.0]``.

    Sabotage proof (executed): make ``_clamp_unit_interval`` return
    ``value`` unchanged → every real case fails on the 1.7 response.
    Restored.
    """
    parser = factory()
    assert parser.parse(_COMPLIANT) == pytest.approx(0.7), name
    assert parser.parse(_OVER_RANGE) == 1.0, name
