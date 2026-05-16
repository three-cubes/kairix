"""Weighted-sample suite cases by CATEGORY_WEIGHTS, deterministic via seed.

The sampler is pure: same (cases, n, weights, seed) → same output. That
means probe runs are reproducible across machines and across container
restarts, while still mixing categories the way real teaming traffic does.

Sampling shape:
    - Per-category target count = round(n * weight)
    - If suite has fewer cases in a category than the target, sample with
      replacement so the category still gets representation (real teaming
      load has the same query repeated across agents).
    - Order is shuffled deterministically by the seed.

The weighting matches the eval framework's CATEGORY_WEIGHTS:
    recall=25%, temporal=20%, entity=20%, conceptual=15%,
    multi_hop=10%, procedural=10%, classification=0%
"""

from __future__ import annotations

import random
from collections import defaultdict
from typing import Any

# Default category weights — re-exported from the benchmark runner so this
# module doesn't import-cycle with eval/.
_DEFAULT_WEIGHTS: dict[str, float] = {
    "recall": 0.25,
    "temporal": 0.20,
    "entity": 0.20,
    "conceptual": 0.15,
    "multi_hop": 0.10,
    "procedural": 0.10,
    "classification": 0.00,
}


def sample_weighted(
    cases: list[Any],
    n: int,
    seed: int = 0,
    weights: dict[str, float] | None = None,
) -> list[Any]:
    """Return n cases sampled by category weight, shuffled deterministically.

    Args:
        cases: list of suite cases; each must have a ``.category`` attribute.
        n: total number of queries to sample.
        seed: shuffle / sample-with-replacement seed.
        weights: per-category weight dict. Defaults to CATEGORY_WEIGHTS.

    Returns:
        list of n cases (may repeat when a category has fewer cases than its
        target count). Order is shuffled by ``seed``.

    Raises:
        ValueError: when n < 1 or no cases match any positive-weight category.
    """
    if n < 1:
        raise ValueError(f"n must be >= 1; got {n}")
    w = weights or _DEFAULT_WEIGHTS

    by_category: dict[str, list[Any]] = defaultdict(list)
    for case in cases:
        by_category[case.category].append(case)

    rng = random.Random(seed)  # noqa: S311 — sample selection, not cryptographic
    sampled: list[Any] = []
    # Use only categories with positive weight AND at least one case.
    active = {cat: wt for cat, wt in w.items() if wt > 0 and by_category.get(cat)}
    if not active:
        raise ValueError(
            f"no cases match any positive-weight category. cases categories: "
            f"{sorted(by_category.keys())}, weight categories: {sorted(w.keys())}"
        )
    total_weight = sum(active.values())

    # Allocate per-category counts via largest-remainder rounding so the
    # sum exactly equals n.
    raw = {cat: n * (wt / total_weight) for cat, wt in active.items()}
    floor_counts = {cat: int(raw[cat]) for cat in active}
    remainder = n - sum(floor_counts.values())
    by_remainder = sorted(active, key=lambda c: raw[c] - floor_counts[c], reverse=True)
    for cat in by_remainder[:remainder]:
        floor_counts[cat] += 1

    # Sample per category. With replacement if target exceeds available
    # (real teaming load includes repeated queries — that's signal, not bias).
    for cat, target in floor_counts.items():
        pool = by_category[cat]
        if target <= len(pool):
            sampled.extend(rng.sample(pool, target))
        else:
            sampled.extend(rng.choices(pool, k=target))

    rng.shuffle(sampled)
    return sampled
