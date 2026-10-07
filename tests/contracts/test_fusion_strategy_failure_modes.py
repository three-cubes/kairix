"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`FusionStrategy`.

``FusionStrategy.fuse`` merges BM25 and vector result lists. Every body
runs over BOTH shipped strategies — :class:`RRFFusion` and
:class:`BM25PrimaryFusion` — AND the canonical
:class:`tests.fakes.FakeFusion` (F43 behavioural parity).

Parity finding (PLA-472): the previous ``raises`` probe asserted that a
failing fusion RAISES, proved only against ``FakeFusion(raises=...)``.
Both shipped strategies are documented "Never raises": an internal
fusion error (e.g. a backend row missing its ``file`` key) is logged at
WARNING and fused to ``[]``. The SearchPipeline therefore sees a failed
fusion as "no hits". The contract now pins that real observable; the
fake gained a faithful ``swallow_errors=True`` mode.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from kairix.core.protocols import FusionStrategy
from kairix.core.search.fusion import BM25PrimaryFusion, RRFFusion
from tests.fakes import FakeFusion

pytestmark = pytest.mark.contract

# A factory takes ``broken`` — True builds a strategy whose fusion fails
# internally on the given rows.
FusionFactory = Callable[[bool], FusionStrategy]

_IMPLEMENTATIONS: list[tuple[str, FusionFactory]] = [
    ("rrf", lambda _broken: RRFFusion()),
    ("bm25_primary", lambda _broken: BM25PrimaryFusion()),
    (
        "fake",
        lambda broken: FakeFusion(raises=KeyError("file"), swallow_errors=True) if broken else FakeFusion(),
    ),
]
_IDS = [impl[0] for impl in _IMPLEMENTATIONS]


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS, ids=_IDS)
def test_fuse_returns_empty_when_rows_malformed(name: str, factory: FusionFactory) -> None:
    """Rows a backend emitted without the fields fusion keys on make the
    fusion fail internally; the strategy logs and returns ``[]`` (never
    raises) — see the PLA-472 finding in the module docstring.

    Sabotage proof (executed): in ``kairix.core.search.rrf.rrf`` re-raise
    from the ``except Exception`` branch. Re-run: the ``rrf`` case fails
    with KeyError. Restored.
    """
    assert factory(True).fuse([{"id": "a"}], [{"id": "b"}]) == [], name


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS, ids=_IDS)
def test_fuse_returns_empty_when_both_inputs_empty(name: str, factory: FusionFactory) -> None:
    """Empty BM25 + empty vector → empty fused result (not None, not
    a sentinel). Callers iterate without a null check.

    Sabotage proof: change ``rrf``'s ``if not bm25 and not vec: return []``
    to ``return None``. Re-run: the ``rrf`` case's ``== []`` fails.
    Restored.
    """
    assert factory(False).fuse([], []) == [], name
