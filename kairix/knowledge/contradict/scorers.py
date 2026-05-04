"""ContradictionScorer Strategies + Composite aggregator.

The dogfood-reported failure mode (2026-05-02) was a single
"directly contradicts?" prompt missing two important categories:

  - Overstatement: claim asserts a stronger position than evidence
    supports (e.g. "X has a monopoly on Y" against direct counter-
    evidence that other parties also do Y).
  - Status mismatch: claim asserts state X for an entity at a time;
    evidence asserts state Y for the same entity at the same time.

Each category has its own prompt; the composite scorer runs all three
against every candidate and aggregates by max — the most damning
category wins. Output carries the category so callers can filter or
group ("show me only overstatements").

LLM responses are parsed via a single shared parser (``parse_llm_score``)
that's tolerant of preambles around the JSON object the model returns.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


_DIRECT_PROMPT = (
    "You are a knowledge consistency analyst.\n\n"
    "Existing document snippet:\n{candidate}\n\n"
    "New content claim:\n{claim}\n\n"
    "Does the existing snippet *directly* contradict the claim? A direct "
    "contradiction exists when the two statements describe the same entity "
    "or event and cannot both be true (e.g. different facts, conflicting "
    "decisions, mutually exclusive states). Incidental differences or "
    "missing context do NOT count.\n\n"
    'Reply with ONLY a JSON object: {{"score": <0.0-1.0>, "reason": "<one sentence>"}}'
)

_OVERSTATEMENT_PROMPT = (
    "You are a knowledge consistency analyst checking for overstatements.\n\n"
    "Existing document snippet:\n{candidate}\n\n"
    "New content claim:\n{claim}\n\n"
    "Does the claim *overstate* what the evidence supports? An overstatement "
    "exists when the claim asserts a stronger position than the snippet "
    "warrants — e.g. 'X is the only one who can Y' when the snippet shows "
    "others also do Y, or 'always' / 'never' / 'monopoly' / 'exclusive' "
    "claims that the evidence undermines.\n\n"
    'Reply with ONLY a JSON object: {{"score": <0.0-1.0>, "reason": "<one sentence>"}}'
)

_STATUS_MISMATCH_PROMPT = (
    "You are a knowledge consistency analyst checking for status mismatches.\n\n"
    "Existing document snippet:\n{candidate}\n\n"
    "New content claim:\n{claim}\n\n"
    "Do the claim and snippet assert *different states* for the same entity "
    "at the same time? Examples: 'published on LinkedIn' vs evidence the "
    "article is unpublished; 'engagement is active' vs evidence it has "
    "closed; 'shipped' vs evidence it is still in design. Focus on "
    "factual status claims, not opinions.\n\n"
    'Reply with ONLY a JSON object: {{"score": <0.0-1.0>, "reason": "<one sentence>"}}'
)


def parse_llm_score(raw: str) -> tuple[float | None, str]:
    """Parse a {"score": float, "reason": str} object out of an LLM response.

    Tolerant of preamble/markdown around the JSON. Returns (None, "") on
    any parse failure so callers can distinguish "the LLM said 0" from
    "the LLM response was unparseable". Score is clamped to [0.0, 1.0]
    when present.

    Never raises.
    """
    if not raw:
        return None, ""

    match = re.search(r"\{[^{}]*\}", raw, re.DOTALL)
    if not match:
        return None, ""

    try:
        obj = json.loads(match.group())
    except json.JSONDecodeError:
        return None, ""

    score_raw = obj.get("score")
    reason = str(obj.get("reason", ""))

    if score_raw is None:
        return None, ""

    try:
        score = float(score_raw)
    except (TypeError, ValueError):
        return None, ""

    return max(0.0, min(1.0, score)), reason


class _PromptedScorer:
    """Shared base for the three single-category scorers — Strategy pattern."""

    category = ""
    _prompt = ""

    def __init__(self, llm: Any) -> None:
        self._llm = llm

    def score(self, claim: str, candidate: str) -> tuple[float, str]:
        prompt = self._prompt.format(claim=claim[:1000], candidate=candidate[:800])
        try:
            raw = self._llm.chat([{"role": "user", "content": prompt}])
        except Exception as exc:
            logger.debug("contradict: %s LLM call failed — %s", self.category, exc)
            return 0.0, ""
        score, reason = parse_llm_score(raw)
        if score is None:
            return 0.0, ""
        return score, reason


class DirectContradictionScorer(_PromptedScorer):
    category = "direct"
    _prompt = _DIRECT_PROMPT


class OverstatementScorer(_PromptedScorer):
    category = "overstatement"
    _prompt = _OVERSTATEMENT_PROMPT


class StatusMismatchScorer(_PromptedScorer):
    category = "status_mismatch"
    _prompt = _STATUS_MISMATCH_PROMPT


class CompositeContradictionScorer:
    """Aggregates multiple ContradictionScorer Strategies.

    Runs every wrapped scorer against the (claim, candidate) pair and
    returns the highest-scoring category along with its reason. The
    ``category`` attribute is the dynamically-determined winner per call;
    when constructed it defaults to "composite" for protocol surface
    purposes.

    score_all() exposes the per-category breakdown for callers that want
    to render category-specific output.
    """

    category = "composite"

    def __init__(self, scorers: list[Any]) -> None:
        self._scorers = list(scorers)

    def score(self, claim: str, candidate: str) -> tuple[float, str]:
        best_score = 0.0
        best_reason = ""
        for scorer in self._scorers:
            s, r = scorer.score(claim, candidate)
            if s > best_score:
                best_score = s
                best_reason = r
        return best_score, best_reason

    def score_all(self, claim: str, candidate: str) -> dict[str, tuple[float, str]]:
        """Per-category breakdown: { category: (score, reason) }."""
        return {scorer.category: scorer.score(claim, candidate) for scorer in self._scorers}

    def best_category(self, claim: str, candidate: str) -> tuple[str, float, str]:
        """Return (winning_category, score, reason). Category is "" when no scorer fired."""
        breakdown = self.score_all(claim, candidate)
        winner = ""
        best_score = 0.0
        best_reason = ""
        for cat, (s, r) in breakdown.items():
            if s > best_score:
                winner = cat
                best_score = s
                best_reason = r
        return winner, best_score, best_reason


def default_contradiction_scorer(llm: Any) -> CompositeContradictionScorer:
    """Production composite covering all three categories — the common case."""
    return CompositeContradictionScorer(
        scorers=[
            DirectContradictionScorer(llm),
            OverstatementScorer(llm),
            StatusMismatchScorer(llm),
        ]
    )
