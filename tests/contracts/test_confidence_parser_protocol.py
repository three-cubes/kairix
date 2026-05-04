"""Contract tests: ConfidenceParser protocol conformance."""

from __future__ import annotations

import pytest

from kairix.agents.research.confidence import (
    ChainedConfidenceParser,
    JsonModeConfidenceParser,
    RegexExtractConfidenceParser,
)
from kairix.agents.research.protocols import ConfidenceParser


class _FakeConfidenceParser:
    """Minimal in-test fake satisfying the ConfidenceParser protocol."""

    def parse(self, response: str) -> float:
        return 0.42


@pytest.mark.contract
def test_json_mode_parser_satisfies_protocol() -> None:
    assert isinstance(JsonModeConfidenceParser(), ConfidenceParser)


@pytest.mark.contract
def test_regex_extract_parser_satisfies_protocol() -> None:
    assert isinstance(RegexExtractConfidenceParser(), ConfidenceParser)


@pytest.mark.contract
def test_chained_parser_satisfies_protocol() -> None:
    chain = ChainedConfidenceParser([JsonModeConfidenceParser()])
    assert isinstance(chain, ConfidenceParser)


@pytest.mark.contract
def test_in_test_fake_satisfies_protocol() -> None:
    assert isinstance(_FakeConfidenceParser(), ConfidenceParser)
