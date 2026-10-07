"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`Scorer`.

``Scorer.score(run)`` turns one captured query run into a metric verdict.
The run-error path is reified: when the query timed out (or otherwise
failed) the scorer receives a ``QueryRunResult`` with ``error`` populated
and must return ``score=0.0`` with the error surfaced in ``details`` —
even when the (stale) ranked output would otherwise have scored a hit.
``Scorer.name`` is the registry key: a lookup miss must raise the F21
``KeyError`` that lists the registered names, so an operator sees which
scorer *is* wired.

One body per method, run over every per-query production scorer (NDCG,
Hit@K, MRR, LLM judge) — the Protocol has no separate test double, so
parity is proved across the real implementations (F43 limb 2).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from kairix.quality.scoring.hit_at_k import HitAtKScorer
from kairix.quality.scoring.llm_judge import LLMJudgeScorer
from kairix.quality.scoring.mrr import MRRScorer
from kairix.quality.scoring.ndcg import NDCGScorer
from kairix.quality.scoring.registry import ScorerRegistry
from kairix.quality.scoring.types import QueryRunResult
from tests.fakes import FakeLLMBackend

pytestmark = pytest.mark.contract

_GOLD = [{"title": "deploy-runbook", "relevance": 2}]
_ANSWER = "Roll the worker first, then the MCP server."

_FACTORIES: list[Callable[[], Any]] = [
    lambda: NDCGScorer(gold_titles=_GOLD),
    lambda: HitAtKScorer(gold_titles=_GOLD),
    lambda: MRRScorer(gold_titles=_GOLD),
    lambda: LLMJudgeScorer(llm=FakeLLMBackend(chat_response="1.0"), expected_answer=_ANSWER),
]
_IDS = ["ndcg", "hit_at_k", "mrr", "llm_judge"]


@pytest.mark.parametrize("factory", _FACTORIES, ids=_IDS)
def test_score_times_out_run_scores_zero_with_error_detail(factory: Callable[[], Any]) -> None:
    """A timed-out run scores 0.0 with ``details["error"]`` carrying the
    failure, although its ranked titles / answer would otherwise be a hit.

    Sabotage proof: in ``NDCGScorer.score`` delete the ``if run.error:``
    early return. Re-run: the ``ndcg`` case fails because the stale ranked
    titles score 1.0. Restored.
    """
    scorer = factory()
    run = QueryRunResult(
        query_id="ops-1",
        category="procedural",
        query_text="how do I deploy?",
        ranked_doc_ids=("ops/deploy-runbook.md",),
        ranked_doc_titles=("deploy-runbook",),
        synthesised_answer=_ANSWER,
        error="search timed out after 30s",
    )

    result = scorer.score(run)

    assert result.score == 0.0
    assert result.details["reason"] == "query_run_failed"
    assert result.details["error"] == "search timed out after 30s"


@pytest.mark.parametrize("factory", _FACTORIES, ids=_IDS)
def test_name_raises_keyerror_listing_registered_name_on_lookup_miss(factory: Callable[[], Any]) -> None:
    """Looking up an unregistered name raises ``KeyError`` whose message
    lists the scorer that IS registered under its ``name``.

    Sabotage proof: in ``ScorerRegistry.get`` replace the ``raise
    KeyError(...)`` with ``return None``. Re-run: every case fails because
    no ``KeyError`` is raised. Restored.
    """
    scorer = factory()
    registry = ScorerRegistry([scorer])

    assert registry.get(scorer.name) is scorer
    with pytest.raises(KeyError, match=f"registered scorers are: {scorer.name}"):
        registry.get("not-a-scorer")
