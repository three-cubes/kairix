"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`ContradictionScorer`.

Single Protocol method ``score(claim, candidate)``. The Protocol
docstring pins the failure contract: implementations MUST NOT raise on
parse failure — return ``(0.0, "")`` instead so the composite can
aggregate cleanly. This makes ``returns_empty`` (zero score + empty
reason) the canonical failure-mode signal. A programming bug inside a
scorer, by contrast, MUST propagate.

Every body runs over BOTH the production
:class:`CompositeContradictionScorer` (wrapping the shipped prompted
scorers, driven by a :class:`tests.fakes.FakeLLMBackend`) and the
canonical :class:`tests.fakes.FakeContradictionScorer` (F43 behavioural
parity).

Each test carries a "Sabotage proof:" comment describing the mutation
that proves the assertion has teeth.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from kairix.knowledge.contradict.protocols import ContradictionScorer
from kairix.knowledge.contradict.scorers import (
    CompositeContradictionScorer,
    DirectContradictionScorer,
    OverstatementScorer,
    StatusMismatchScorer,
)
from tests.fakes import FakeContradictionScorer, FakeLLMBackend

pytestmark = pytest.mark.contract

# A factory takes the bug an inner scorer hits (``None`` = healthy).
ScorerFactory = Callable[[BaseException | None], ContradictionScorer]


def _real_scorer(bug: BaseException | None) -> ContradictionScorer:
    # The LLM answers "no contradiction" for every category.
    llm = FakeLLMBackend(chat_response='{"score": 0.0, "reason": ""}')
    inner: list[object] = [DirectContradictionScorer(llm), OverstatementScorer(llm), StatusMismatchScorer(llm)]
    if bug is not None:
        inner.append(FakeContradictionScorer(raises=bug))
    return CompositeContradictionScorer(scorers=inner)


def _fake_scorer(bug: BaseException | None) -> ContradictionScorer:
    return FakeContradictionScorer(raises=bug)


_IMPLEMENTATIONS: list[tuple[str, ScorerFactory]] = [
    ("real", _real_scorer),
    ("fake", _fake_scorer),
]


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_score_returns_empty_when_no_scorer_finds_contradiction(name: str, factory: ScorerFactory) -> None:
    """When no category finds a contradiction the scorer MUST return
    ``(0.0, "")`` — the empty-tuple contract proves "no contradiction
    detected" is the documented signal callers can distinguish from a
    raised exception.

    Sabotage proof (executed): in :meth:`CompositeContradictionScorer.score`
    change the initial ``best_score = 0.0`` to ``best_score = 0.5``.
    Re-run: the ``real`` case fails because the result is ``(0.5, "")``.
    Restored.
    """
    score, reason = factory(None).score(claim="Alpha shipped.", candidate="Alpha is shipping later.")
    assert score == 0.0, f"{name}: no-contradiction must aggregate to 0.0; got {score}"
    assert reason == "", f"{name}: no-contradiction must yield empty reason; got {reason!r}"


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_score_raises_when_inner_scorer_implementation_crashes(name: str, factory: ScorerFactory) -> None:
    """The "MUST NOT raise on parse failure" contract covers controlled
    LLM-non-compliance — a programming bug in a scorer (e.g.
    ``ZeroDivisionError``) MUST still surface so the operator sees the
    crash rather than getting silent zeros.

    Sabotage proof: in :meth:`CompositeContradictionScorer.score` wrap
    the ``s, r = scorer.score(...)`` call in
    ``try: ... except Exception: s, r = 0.0, ""``. Re-run: the ``real``
    case fails because the exception is swallowed. Restored.
    """
    with pytest.raises(RuntimeError, match="F68-scorer-bug"):
        factory(RuntimeError("F68-scorer-bug")).score(claim="a", candidate="b")
