"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`FactExtractor`.

``FactExtractor.extract`` converts a window of conversation turns into
zero or more :class:`FactRecord` items. The Protocol explicitly
documents that an empty-list return is a valid "no facts groundable"
signal — callers MUST tolerate it without raising. The two canonical
failure shapes pinned below:

  * **returns_empty** — no facts in the turn window (most common path)
  * **raises** — backend / parsing failure surfaces to the caller

Every body runs over BOTH the production
:class:`kairix.core.facts.extractor.LLMFactExtractor` (its LLM injected
with :class:`tests.fakes.FakeLLMBackend`) and the canonical
:class:`tests.fakes.FakeFactExtractor` (F43 behavioural parity).

Parity finding (PLA-472): ``FakeFactExtractor`` returned its scripted
facts even for an EMPTY turn window, while the production extractor
short-circuits to ``[]`` before any LLM call. The fake was made faithful.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from kairix.core.facts.extractor import LLMFactExtractor
from kairix.core.protocols import FactExtractor
from tests.fakes import FakeFactExtractor, FakeFactRecord, FakeLLMBackend

pytestmark = pytest.mark.contract

_FACT_JSON = (
    '[{"entity": "agent-alpha", "attribute": "role", "value": "reviewer", '
    '"confidence": 0.9, "evidence_turn_ids": ["t1"]}]'
)

# A factory takes the scenario: "none" (backend finds no facts), "one"
# (backend finds one fact), or "crash" (backend raises).
ExtractorFactory = Callable[[str], FactExtractor]


def _real(scenario: str) -> FactExtractor:
    if scenario == "crash":
        return LLMFactExtractor(llm=FakeLLMBackend(chat_raises=RuntimeError("F68-extract-raises")))
    return LLMFactExtractor(llm=FakeLLMBackend(chat_response="[]" if scenario == "none" else _FACT_JSON))


def _fake(scenario: str) -> FactExtractor:
    if scenario == "crash":
        return FakeFactExtractor(raises=RuntimeError("F68-extract-raises"))
    facts = [] if scenario == "none" else [FakeFactRecord(id="f1", entity="agent-alpha", attribute="role", value="x")]
    return FakeFactExtractor(scripted_facts=facts)


_IMPLEMENTATIONS: list[tuple[str, ExtractorFactory]] = [
    ("real", _real),
    ("fake", _fake),
]

_TURNS = [{"id": "t1", "speaker": "agent-alpha", "content": "hello"}]


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_extract_returns_empty_when_no_facts_groundable(name: str, factory: ExtractorFactory) -> None:
    """An empty-list return is valid — callers iterate without a null
    check.

    Sabotage proof: in ``LLMFactExtractor.extract`` return ``None`` when
    no payloads parse. Re-run: the ``real`` case's ``== []`` fails.
    Restored.
    """
    out = factory("none").extract(turns=_TURNS)
    assert out == [], name
    assert isinstance(out, list), name


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_extract_raises_propagates_typed_exception(name: str, factory: ExtractorFactory) -> None:
    """A failing backend (LLM error, network blip) surfaces the exception
    so the ingest pipeline can dead-letter the window. The Protocol does
    NOT allow silent fallback to ``[]`` — that would mask backend
    failures as "no facts".

    Sabotage proof (executed): in ``LLMFactExtractor.extract`` wrap the
    ``self._llm.chat(...)`` call in ``try/except Exception: return []``.
    Re-run: the ``real`` case fails. Restored.
    """
    with pytest.raises(RuntimeError, match="F68-extract-raises"):
        factory("crash").extract(turns=_TURNS)


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_extract_returns_empty_when_turns_empty(name: str, factory: ExtractorFactory) -> None:
    """An empty window is a degenerate "no facts groundable" case — the
    extractor returns ``[]`` without raising (and without consulting the
    backend), even when the backend WOULD have produced facts.

    Sabotage proof (executed): drop the ``if not turns: return []``
    short-circuit in ``LLMFactExtractor.extract``. Re-run: the ``real``
    case fails because the LLM's fact is returned. Restored.
    """
    extractor = factory("one")
    assert extractor.extract(turns=[]) == [], name
    # Positive control: the same backend DOES ground a fact for a real window.
    assert len(extractor.extract(turns=_TURNS)) == 1, name
