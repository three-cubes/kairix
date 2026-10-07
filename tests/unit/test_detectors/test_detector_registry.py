"""Unit tests for :func:`kairix.core.agents.detectors.get_registered_detectors`
(PR 1.3 / #420).

The registry is the production wiring of the shipped detectors — a single
concrete function, not a Protocol contract — so its ordering promises live
here rather than in ``tests/contracts/test_harness_detector_protocol.py``.
"""

from __future__ import annotations

import pytest

from kairix.core.agents.detectors import GenericDetector, get_registered_detectors

pytestmark = pytest.mark.unit


# Sabotage-proof (executed): reordered the registry tuple in
# ``get_registered_detectors`` to put generic first → the names assertion
# failed; restored.
def test_registry_returns_three_detectors_with_generic_last() -> None:
    """``get_registered_detectors`` returns exactly the three known
    detectors. ``generic`` is always last so callers iterating the tuple
    can treat it as the fallback shape (no special branching)."""
    detectors = get_registered_detectors()
    assert isinstance(detectors, tuple)
    assert len(detectors) == 3
    names = [d.name for d in detectors]
    assert names == ["claude-code", "codex", "generic"]
    assert isinstance(detectors[-1], GenericDetector)


# Sabotage-proof (executed): made ``get_registered_detectors`` return the
# tuple reversed on every second call → ``first == second`` failed; restored.
def test_registry_order_is_deterministic_across_calls() -> None:
    """The registry returns the detectors in the same order on every
    call — callers iterating multiple times must see the same proposal
    sequence."""
    first = [d.name for d in get_registered_detectors()]
    second = [d.name for d in get_registered_detectors()]
    assert first == second
