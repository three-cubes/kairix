"""Unit tests for `kairix.quality.probe.sampler.sample_weighted`.

Each test pins one observable property: determinism, weight allocation,
sample-with-replacement on small pools, and rejection cases. Sabotage-prove
means each test fails if the corresponding logic is removed or weakened
(e.g. swap weights, drop the seed, replace with-replacement with a raise).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

import pytest

from kairix.quality.probe.sampler import sample_weighted

pytestmark = pytest.mark.unit


@dataclass(frozen=True)
class _Case:
    """Minimal stand-in for BenchmarkCase — only `.category` is read."""

    id: str
    category: str


def _build_cases(per_category: dict[str, int]) -> list[_Case]:
    cases: list[_Case] = []
    for cat, n in per_category.items():
        for i in range(n):
            cases.append(_Case(id=f"{cat}-{i}", category=cat))
    return cases


def test_same_seed_returns_identical_order() -> None:
    """Two calls with the same seed produce the same sequence — reproducibility.

    Sabotage-proof: remove the seeded ``rng.shuffle(sampled)`` and the two
    runs diverge.
    """
    cases = _build_cases({"recall": 10, "temporal": 10, "entity": 10})
    a = sample_weighted(cases, n=15, seed=42)
    b = sample_weighted(cases, n=15, seed=42)
    assert [c.id for c in a] == [c.id for c in b]


def test_different_seeds_diverge() -> None:
    """Different seeds produce different orderings (rules out a constant order).

    Sabotage-proof: replace ``rng.shuffle`` with ``list.sort`` and this fails.
    """
    cases = _build_cases({"recall": 10, "temporal": 10, "entity": 10})
    a = sample_weighted(cases, n=15, seed=1)
    b = sample_weighted(cases, n=15, seed=2)
    assert [c.id for c in a] != [c.id for c in b]


def test_output_size_matches_n_exactly() -> None:
    """Largest-remainder rounding guarantees ``len(out) == n`` for any n.

    Sabotage-proof: replace largest-remainder with plain ``round()`` and odd
    ns leave off-by-one gaps.
    """
    cases = _build_cases({"recall": 10, "temporal": 10, "entity": 10, "conceptual": 10})
    for n in (1, 7, 13, 25, 100):
        out = sample_weighted(cases, n=n, seed=0)
        assert len(out) == n, f"expected {n} samples, got {len(out)}"


def test_category_distribution_matches_weights() -> None:
    """Per-category counts match the active-weight shares (within rounding).

    At n=100 with the active four weights normalised to 1.0, the largest-
    remainder rounding pins each count to its target exactly. Sabotage-proof:
    flip the weights dict to uniform and the assertion fails.
    """
    weights = {"recall": 0.25, "temporal": 0.25, "entity": 0.25, "conceptual": 0.25}
    cases = _build_cases({"recall": 50, "temporal": 50, "entity": 50, "conceptual": 50})
    out = sample_weighted(cases, n=100, seed=0, weights=weights)
    counts = Counter(c.category for c in out)
    for cat in weights:
        assert counts[cat] == 25, f"{cat}: expected 25, got {counts[cat]}"


def test_categories_with_zero_weight_are_excluded() -> None:
    """A category with weight=0 contributes nothing to the sample.

    Sabotage-proof: drop the ``wt > 0`` filter and classification leaks in.
    """
    weights = {"recall": 0.5, "temporal": 0.5, "classification": 0.0}
    cases = _build_cases({"recall": 10, "temporal": 10, "classification": 10})
    out = sample_weighted(cases, n=20, seed=0, weights=weights)
    assert all(c.category != "classification" for c in out)


def test_samples_with_replacement_when_pool_smaller_than_target() -> None:
    """When the target exceeds the available pool, the same case appears more than once.

    Real teaming load includes repeated queries — that's signal, not bias.
    Sabotage-proof: replace ``rng.choices`` with ``rng.sample`` (no
    replacement) and this raises ValueError on the small pool.
    """
    weights = {"recall": 1.0}
    cases = _build_cases({"recall": 3})
    out = sample_weighted(cases, n=10, seed=0, weights=weights)
    assert len(out) == 10
    ids = [c.id for c in out]
    assert len(set(ids)) < len(ids), "expected repeats when target exceeds pool"


def test_n_less_than_one_raises() -> None:
    """n<1 has no defensible meaning — reject early.

    Sabotage-proof: remove the guard and ``sample_weighted([], 0)`` returns
    [] silently instead of telling the caller they asked for nothing.
    """
    cases = _build_cases({"recall": 5})
    with pytest.raises(ValueError, match="n must be >= 1"):
        sample_weighted(cases, n=0, seed=0)


def test_no_active_categories_raises() -> None:
    """No positive-weight category has any cases → operator misconfig; raise.

    Sabotage-proof: remove the ``active`` check and the function silently
    returns []. Tests downstream would see an empty workload and pass.
    """
    weights = {"recall": 1.0}
    cases = _build_cases({"procedural": 5})  # only procedural cases, but weight=0
    with pytest.raises(ValueError, match="no cases match any positive-weight category"):
        sample_weighted(cases, n=5, seed=0, weights=weights)


def test_missing_category_skipped_not_failed() -> None:
    """A weight key with no matching cases is dropped; other categories absorb the share.

    The benchmark suite may omit a category entirely; the sampler must keep
    going on the remaining ones. Sabotage-proof: remove the ``by_category.get(cat)``
    filter and the function raises KeyError when sampling the empty category.
    """
    weights = {"recall": 0.5, "temporal": 0.3, "missing_cat": 0.2}
    cases = _build_cases({"recall": 10, "temporal": 10})
    out = sample_weighted(cases, n=10, seed=0, weights=weights)
    assert len(out) == 10
    assert {c.category for c in out} == {"recall", "temporal"}


def test_default_weights_match_eval_framework() -> None:
    """Calling with weights=None produces a distribution that matches CATEGORY_WEIGHTS.

    Probe and benchmark must agree on relative category importance — otherwise
    the probe's measurements wouldn't generalise to what the eval suite scores.
    At n=200 with an evenly-supplied case pool, largest-remainder rounding
    pins each non-zero-weight category to its target share.

    Sabotage-proof: edit the default weights away from the documented shape
    and at least one expected count drifts. Drives the check through the
    public ``sample_weighted`` surface — no underscore-import.
    """
    cases = _build_cases(
        {"recall": 200, "temporal": 200, "entity": 200, "conceptual": 200, "multi_hop": 200, "procedural": 200}
    )
    out = sample_weighted(cases, n=200, seed=0)
    counts = Counter(c.category for c in out)
    expected = {
        "recall": 50,
        "temporal": 40,
        "entity": 40,
        "conceptual": 30,
        "multi_hop": 20,
        "procedural": 20,
    }
    assert dict(counts) == expected, f"default-weight distribution drifted: {dict(counts)}"
    assert counts.get("classification", 0) == 0, "classification weight is 0 — must not appear"
