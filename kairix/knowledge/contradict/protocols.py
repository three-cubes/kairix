"""Protocols for the contradict pipeline.

  - ``ClaimExtractor``: split content into a small set of high-signal
    claims that the search step retrieves candidates against. Replaces
    the historical 500-char truncation in detector.py.
  - ``ContradictionScorer``: score a (claim, candidate) pair on one
    contradiction category. Three Strategies — direct, overstatement,
    status mismatch — compose into a CompositeContradictionScorer
    aggregating by max.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ClaimExtractor(Protocol):
    """Splits raw content into top-N claims for retrieval scoring.

    Implementations rank by entity density, modal-verb presence, or any
    heuristic that surfaces the load-bearing assertions in the input.
    Returns at most ``top_n`` claim strings; may return fewer for short
    inputs.
    """

    def extract(self, content: str, *, top_n: int = 3) -> list[str]: ...


@runtime_checkable
class ContradictionScorer(Protocol):
    """Score a single (claim, candidate) pair on one contradiction category.

    Each implementation owns one ``category`` (e.g. "direct",
    "overstatement", "status_mismatch") and one prompt template. Returns
    a (score, reason) tuple where score is in [0.0, 1.0]. Implementations
    must NOT raise on parse failure — return (0.0, "") instead so the
    composite can aggregate cleanly.
    """

    category: str

    def score(self, claim: str, candidate: str) -> tuple[float, str]: ...
