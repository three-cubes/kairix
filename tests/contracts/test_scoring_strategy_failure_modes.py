"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`ScoringStrategy`.

One method (``score``). Failure surface:

  * ``raises`` — gold-parse failure surfaces verbatim; caller must not
    interpret a silently-zero score as "no relevance".
  * ``returns_empty`` — when ``retrieved`` is empty the score is 0.0
    (sentinel for "nothing to score"), NOT a raise.

F43: every test runs ONE assertion body over the real
:class:`kairix.quality.eval.scorers.NDCGScorer` AND
:class:`tests.fakes.FakeScoringStrategy`.

The raises leg pins the graded-gold scorer, which propagates a malformed
relevance grade. :class:`~kairix.quality.eval.scorers.LLMJudgeScorer`
honours the same contract — a backend / parse failure raises
``JudgeFailedError`` rather than returning a silent ``0.0`` (pinned in
``tests/eval/test_scorers_units.py``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from kairix.core.protocols import ScoringStrategy
from kairix.quality.eval.scorers import NDCGScorer
from tests.fakes import FakeScoringStrategy

pytestmark = pytest.mark.contract

# A gold list whose relevance grade cannot be parsed as a number — the
# real scorer's DCG arithmetic raises TypeError on it.
_MALFORMED_GOLD: list[dict[str, Any]] = [{"path": "a.md", "relevance": "not-a-grade"}]
_GOLD: list[dict[str, Any]] = [{"path": "a.md", "relevance": 2}]

ScorerFactory = Callable[[BaseException | None], ScoringStrategy]


def _real(_raises: BaseException | None) -> ScoringStrategy:
    """Real NDCG scorer — failure is driven by the malformed gold input."""
    return NDCGScorer(k=10)


def _fake(raises: BaseException | None) -> ScoringStrategy:
    return FakeScoringStrategy(score=0.7, raises=raises)


_IMPLS = [_real, _fake]


@pytest.mark.parametrize("factory", _IMPLS, ids=["real", "fake"])
def test_score_raises_propagates_typed_exception(factory: ScorerFactory) -> None:
    """A gold-parse failure surfaces — caller must not interpret silent
    0.0 as "no relevance" when the scorer crashed mid-evaluation.

    Sabotage proof: in ``kairix.quality.eval.scorers.NDCGScorer.score``
    wrap the ``ndcg_graded`` call in ``try/except Exception: return 0.0``.
    Re-run: the real leg's pytest.raises sees nothing. Restored.
    """
    scorer = factory(TypeError("unsupported operand type(s) for /: 'str' and 'float'"))
    with pytest.raises(TypeError):
        scorer.score(retrieved=["a.md"], gold=_MALFORMED_GOLD)


@pytest.mark.parametrize("factory", _IMPLS, ids=["real", "fake"])
def test_score_returns_empty_when_retrieved_list_empty(factory: ScorerFactory) -> None:
    """Empty retrieved list yields 0.0 — sentinel for "nothing to
    score", NOT a raise.

    Sabotage proof: in ``kairix.quality.eval.metrics.ndcg_graded`` add
    ``if not retrieved: return 1.0`` after the gold guard. Re-run: the
    real leg's ``== 0.0`` assertion fails. Restored.
    """
    scorer = factory(None)
    assert scorer.score(retrieved=[], gold=_GOLD) == pytest.approx(0.0)
