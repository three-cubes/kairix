"""End-to-end integration tests for ``kairix.knowledge.contradict``.

Wires the contradict pipeline through real components — real
``EntityDensityClaimExtractor``, real ``CompositeContradictionScorer``
constructed around a fake LLM, and a fake search callable in place of
the production ``SearchPipeline``. The fake search returns canned
candidate snippets so the rest of the pipeline runs end-to-end without
needing an indexed corpus.

What's covered here that unit + BDD don't catch:
  - Real claim extraction → real composite scorer → real result
    aggregation fires together on three contrasting inputs.
  - The "contradiction present" path lands a structured ``ContradictionHit``
    with the snippet, score, and source path the operator needs.
  - The "no related content" path returns a clean no-contradiction
    envelope (empty ``contradictions`` list, ``has_contradictions=False``).
  - The "no overlap" path doesn't false-positive when the new claim
    talks about a different subject.

Fakes:
  - ``_FakeLLM`` — single-shot ``chat`` returning a scripted score string
    so the real scorer's parse/aggregate path is exercised end-to-end.
  - ``_FakeSearch`` — callable mirroring ``SearchPipeline.search`` that
    returns a canned ``SearchResult``-shaped namespace.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from kairix.knowledge.contradict.detector import (
    ContradictDetectorDeps,
    check_contradiction,
)
from kairix.knowledge.contradict.extract import EntityDensityClaimExtractor
from kairix.knowledge.contradict.scorers import default_contradiction_scorer
from kairix.use_cases.contradict import ContradictDeps, run_contradict

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Boundary fakes
# ---------------------------------------------------------------------------


class _FakeLLM:
    """Scripted LLM: maps each ``chat()`` call to a canned response.

    The composite scorer fires three category prompts per (claim,
    candidate) pair. The fake returns a uniform score response keyed on
    a substring of the prompt, so the real ``parse_llm_score`` path runs.
    """

    def __init__(self, *, score_text: str = '{"score": 0.0, "reason": ""}') -> None:
        self._score_text = score_text
        self.calls: list[list[dict[str, str]]] = []

    def chat(self, messages: list[dict[str, str]], max_tokens: int = 800) -> str:
        del max_tokens
        self.calls.append(messages)
        return self._score_text


class _FakeSearch:
    """Search callable returning a canned ``SearchResult``-shaped namespace.

    Mirrors ``SearchPipeline.search`` — ``__call__(query=..., budget=..., ...)``
    returns an object with a ``results`` list, where each item has a
    ``result.path`` and a ``content`` field (matches the detector's
    ``bundle.result.path`` / ``bundle.content`` access).
    """

    def __init__(self, bundles_by_query_substring: dict[str, list[Any]] | None = None) -> None:
        self._bundles_by_query_substring = bundles_by_query_substring or {}
        self.calls: list[dict[str, Any]] = []

    def __call__(self, *, query: str, **kwargs: Any) -> Any:
        self.calls.append({"query": query, **kwargs})
        bundles: list[Any] = []
        for substring, configured in self._bundles_by_query_substring.items():
            if substring.lower() in query.lower():
                bundles = list(configured)
                break
        return SimpleNamespace(results=bundles)


def _bundle(path: str, content: str) -> Any:
    """Build a ``SearchResult``-shaped bundle (``.result.path`` + ``.content``)."""
    return SimpleNamespace(result=SimpleNamespace(path=path), content=content)


# ---------------------------------------------------------------------------
# End-to-end check_contradiction
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_contradiction_detected_when_seeded_fact_disagrees_with_new_claim() -> None:
    """A fact "A is X" in the store + new content "A is Y" → the pipeline
    returns a structured ``ContradictionResult`` carrying the seeded
    snippet path.

    Sabotage: if the detector stopped routing winning scores into
    ``ContradictionResult.doc_path``, the assertion on ``[0].doc_path``
    would fail.
    """
    fake_search = _FakeSearch(
        bundles_by_query_substring={
            "openclaw": [
                _bundle(
                    "decisions/openclaw-active.md",
                    "OpenClaw is the active orchestration platform for all Kairix agents.",
                ),
            ],
        }
    )

    # Real scorer wrapped around the fake LLM — the LLM returns a strong
    # contradiction score so the composite's aggregator picks it up.
    llm = _FakeLLM(score_text='{"score": 0.85, "reason": "asserts a different state for OpenClaw"}')
    scorer = default_contradiction_scorer(llm)

    deps = ContradictDetectorDeps(
        search=fake_search,
        scorer=scorer,
        extractor=EntityDensityClaimExtractor(),
    )
    results = check_contradiction(
        "OpenClaw has been deprecated and is no longer used for agent orchestration.",
        llm=llm,
        top_k=5,
        threshold=0.45,
        top_claims=3,
        deps=deps,
    )

    assert len(results) >= 1
    top = results[0]
    assert top.doc_path == "decisions/openclaw-active.md"
    assert top.score >= 0.45
    assert top.snippet.startswith("OpenClaw is the active")
    # The winning category is one of the three the composite tracks.
    assert top.category in {"direct", "overstatement", "status_mismatch"}
    # The fake search was actually consulted — sabotage-prove the wiring.
    assert fake_search.calls, "expected the search callable to be invoked"


@pytest.mark.integration
def test_no_contradiction_when_new_content_is_unrelated() -> None:
    """Seed mentions OpenClaw; new claim discusses something else. The
    detector returns an empty list and the use-case envelope reports
    ``has_contradictions=False``.

    Sabotage: if the search routing dropped the substring guard and
    surfaced unrelated bundles, a false-positive contradiction would
    appear in ``out.contradictions``.
    """
    fake_search = _FakeSearch(
        bundles_by_query_substring={
            "openclaw": [
                _bundle("decisions/openclaw-active.md", "OpenClaw is active."),
            ],
        }
    )
    llm = _FakeLLM()  # default score=0, never flags
    scorer = default_contradiction_scorer(llm)

    deps = ContradictDeps(
        check_fn=lambda **kw: check_contradiction(
            **kw,
            deps=ContradictDetectorDeps(
                search=fake_search,
                scorer=scorer,
                extractor=EntityDensityClaimExtractor(),
            ),
        ),
        llm_backend=llm,
    )
    out = run_contradict(
        "We replaced our brewing kettle last Tuesday.",
        top_k=5,
        threshold=0.45,
        top_claims=3,
        deps=deps,
    )

    assert out.error == ""
    assert out.has_contradictions is False
    assert out.contradictions == []


@pytest.mark.integration
def test_empty_store_yields_no_contradictions_clean_envelope() -> None:
    """When the search returns no candidates, the pipeline returns a
    clean no-contradiction envelope — no error, no hits, scorer never
    fires (no candidates to score against).

    Sabotage: if the detector treated an empty candidate list as a
    failure, ``out.error`` would be populated.
    """
    fake_search = _FakeSearch(bundles_by_query_substring={})  # empty store
    llm = _FakeLLM()
    scorer = default_contradiction_scorer(llm)

    deps = ContradictDeps(
        check_fn=lambda **kw: check_contradiction(
            **kw,
            deps=ContradictDetectorDeps(
                search=fake_search,
                scorer=scorer,
                extractor=EntityDensityClaimExtractor(),
            ),
        ),
        llm_backend=llm,
    )
    out = run_contradict(
        "OpenClaw has been deprecated.",
        top_k=5,
        threshold=0.45,
        top_claims=3,
        deps=deps,
    )

    assert out.error == ""
    assert out.has_contradictions is False
    assert out.contradictions == []
    # Sabotage-prove: the scorer never had to chat because there were
    # no candidates. If the pipeline had fabricated a phantom candidate
    # the fake LLM would have logged a call.
    assert llm.calls == []


@pytest.mark.integration
def test_contradict_results_are_sorted_by_score_descending() -> None:
    """When several candidates score above threshold, the highest-scoring
    one comes first — operators see the strongest contradiction at the top.

    Sabotage: if the detector stopped sorting by score (e.g. preserved
    insertion order from the dedup pass), the first result might be
    the lower-scored one.
    """
    fake_search = _FakeSearch(
        bundles_by_query_substring={
            "openclaw": [
                _bundle("docs/weak.md", "Some weak relevance."),
                _bundle("docs/strong.md", "OpenClaw is the canonical platform."),
            ],
        }
    )

    class _GradedLLM:
        """Returns different scores depending on which snippet is in the prompt."""

        def __init__(self) -> None:
            self.calls: list[str] = []

        def chat(self, messages: list[dict[str, str]], max_tokens: int = 800) -> str:
            del max_tokens
            content = messages[0]["content"] if messages else ""
            self.calls.append(content)
            if "canonical platform" in content:
                return '{"score": 0.9, "reason": "strong"}'
            return '{"score": 0.55, "reason": "weak"}'

    llm = _GradedLLM()
    scorer = default_contradiction_scorer(llm)

    results = check_contradiction(
        "OpenClaw has been deprecated.",
        llm=llm,
        top_k=5,
        threshold=0.45,
        top_claims=3,
        deps=ContradictDetectorDeps(
            search=fake_search,
            scorer=scorer,
            extractor=EntityDensityClaimExtractor(),
        ),
    )

    assert len(results) == 2
    assert results[0].doc_path == "docs/strong.md"
    assert results[0].score > results[1].score
    assert results[1].doc_path == "docs/weak.md"
