"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`IntentClassifier`.

The production ``classify`` function in ``kairix.core.search.intent`` is
deliberately never-raises — its ``try/except`` collapses any internal
exception to ``QueryIntent.SEMANTIC`` (the conservative default that lets
the SearchPipeline keep running on malformed input).

That makes the canonical F68 failure class for this Protocol
**returns_empty** — the boundary observable when there is "nothing
classifiable" in the input. The contract is:

  empty / whitespace-only / explosive input → SEMANTIC (the
  semantically-empty intent that lets BM25 + vector fan-out without
  any intent-specific routing).

F43 parity: both bodies run over the real classifier
(:class:`tests.fakes.RealClassifierAdapter`, which delegates to the
production :func:`kairix.core.search.intent.classify` exactly as
``kairix.core.factory``'s ``_RuleClassifier`` shim does) AND the
canonical :class:`tests.fakes.FakeClassifier` at its default
(SEMANTIC) configuration.

Finding (fake-vs-real drift, reported not changed):
``FakeClassifier(raises=...)`` RAISES from ``classify`` while the real
classifier never raises. That knob models a hypothetical misbehaving
classifier for caller-robustness tests; it has no production analogue.

Sabotage proofs are recorded inline next to each test.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from kairix.core.protocols import IntentClassifier
from kairix.core.search.intent import QueryIntent
from tests.fakes import FakeClassifier, RealClassifierAdapter

pytestmark = pytest.mark.contract

_IMPLS: list[Callable[[], IntentClassifier]] = [RealClassifierAdapter, FakeClassifier]
_IDS = ["real", "fake"]


class _ExplodingQuery(str):
    """A ``str`` whose ``strip()`` raises — drives the classifier's
    internal-exception path through the public surface (no patching)."""

    def strip(self, chars: str | None = None) -> str:
        del chars
        raise RuntimeError("F68-intent-raises")


@pytest.mark.parametrize("factory", _IMPLS, ids=_IDS)
def test_classify_returns_empty_on_empty_input_yields_semantic_default(
    factory: Callable[[], IntentClassifier],
) -> None:
    """Empty input is the canonical "returns_empty" path — no tokens to
    route on, so the classifier falls through to ``SEMANTIC`` which
    triggers default BM25 + vector fan-out downstream.

    Sabotage proof: in ``kairix/core/search/intent.py``
    :func:`classify_with_confidence`, change the empty-input return to
    ``IntentDecision(primary=QueryIntent.KEYWORD, ...)``. Re-ran: the real
    leg fails because the assertion expects SEMANTIC. Restored.
    """
    classifier = factory()
    assert classifier.classify("") is QueryIntent.SEMANTIC
    assert classifier.classify("   ") is QueryIntent.SEMANTIC


@pytest.mark.parametrize("factory", _IMPLS, ids=_IDS)
def test_classify_raises_swallowed_returns_semantic_default(factory: Callable[[], IntentClassifier]) -> None:
    """The Protocol's "raises" surface is absorbed by the classifier — an
    internal exception while classifying is caught and downgraded to
    ``SEMANTIC``; a pathological 10k-char query never raises either.

    Sabotage proof: in :func:`classify_with_confidence` change the
    ``except Exception`` fallback to return
    ``IntentDecision(primary=QueryIntent.KEYWORD, ...)``. Re-ran: the real
    leg fails (KEYWORD is not SEMANTIC). Restored.
    """
    classifier = factory()
    assert classifier.classify(_ExplodingQuery("what changed")) is QueryIntent.SEMANTIC
    # A 10k-char pathological query — never raises, always returns a
    # valid QueryIntent (never None, never a string).
    assert isinstance(classifier.classify("x" * 10_000), QueryIntent)
