"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`ContentClassifier`.

``ContentClassifier`` is the two-step (rules, then LLM fallback) surface
the benchmark runner scores classification cases through
(:func:`kairix.quality.benchmark.runner.classification_score`). A
classifier failure must score the case 0.0 — never crash the benchmark
run — and an empty LLM fallback (``unknown``) must flow through as the
final verdict rather than being mistaken for a match.

One body per method, two implementations (F43 limb 2):

* the real :class:`DefaultContentClassifier` (delegates to
  ``kairix.core.classify``), fed a malformed (bytes) query that makes the
  rule engine raise, or an empty query that makes the LLM step
  short-circuit to ``unknown`` without any API call;
* the canonical :class:`tests.fakes.FakeContentClassifier`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from kairix.quality.benchmark.runner import DefaultContentClassifier, classification_score
from tests.fakes import FakeContentClassifier

pytestmark = pytest.mark.contract

_RAISING_FACTORIES: list[Callable[[], Any]] = [
    DefaultContentClassifier,
    lambda: FakeContentClassifier(
        rules_raises=TypeError("cannot use a string pattern on a bytes-like object"),
        llm_type="procedural-rule",
    ),
]
_EMPTY_FACTORIES: list[Callable[[], Any]] = [
    DefaultContentClassifier,
    lambda: FakeContentClassifier(rules_type="unknown", llm_type="unknown"),
]
_IDS = ["real-default-classifier", "fake"]


@pytest.mark.parametrize("factory", _RAISING_FACTORIES, ids=_IDS)
def test_classify_rules_raises_on_malformed_query_case_scores_zero(factory: Callable[[], Any]) -> None:
    """The rule engine raises on a bytes query; the case scores 0.0 and the
    benchmark keeps running.

    Sabotage proof: in ``classification_score`` remove the
    ``try`` / ``except Exception: return 0.0`` guard. Re-run: both cases
    fail with ``TypeError`` escaping into the benchmark loop. Restored.
    """
    classifier = factory()
    malformed: Any = b"how do I deploy the worker?"

    with pytest.raises(TypeError):
        classifier.classify_rules(malformed, agent="shared")

    assert classification_score(malformed, "procedural-rule", classifier=classifier) == 0.0


@pytest.mark.parametrize("factory", _EMPTY_FACTORIES, ids=_IDS)
def test_classify_with_llm_returns_empty_unknown_verdict_flows_to_score(factory: Callable[[], Any]) -> None:
    """With nothing to classify both steps return ``unknown``: the LLM
    fallback's empty verdict is final, so the case only matches an
    ``unknown`` expectation.

    Sabotage proof: in ``kairix/core/classify/judge.py::classify_with_llm``
    change the empty-content short-circuit to ``type="procedural-rule"``.
    Re-run: the real case fails on both score assertions. Restored.
    """
    classifier = factory()

    assert classifier.classify_with_llm("", agent="shared").type == "unknown"
    assert classification_score("", "procedural-rule", classifier=classifier) == 0.0
    assert classification_score("", "unknown", classifier=classifier) == 1.0
