"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`ClaimExtractor`.

Single Protocol method ``extract(content, *, top_n)``. The Protocol
docstring promises "may return fewer for short inputs" — the
``returns_empty`` failure class is observable when ``content`` is blank.
Every body runs over BOTH the shipped
:class:`kairix.knowledge.contradict.extract.EntityDensityClaimExtractor`
and the canonical :class:`tests.fakes.FakeClaimExtractor` (F43
behavioural parity).

Each test carries a "Sabotage proof:" comment describing the mutation
that proves the assertion has teeth.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest

from kairix.knowledge.contradict.extract import EntityDensityClaimExtractor
from kairix.knowledge.contradict.protocols import ClaimExtractor
from tests.fakes import FakeClaimExtractor

pytestmark = pytest.mark.contract

_IMPLEMENTATIONS: list[tuple[str, Callable[[], ClaimExtractor]]] = [
    ("real", EntityDensityClaimExtractor),
    ("fake", FakeClaimExtractor),
]


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_extract_returns_empty_when_content_is_blank(name: str, factory: Callable[[], ClaimExtractor]) -> None:
    """Blank input MUST return an empty list — downstream contradiction
    scorers expect "no claims" as a valid signal (skip the pair, don't
    crash). Non-blank input returns at most ``top_n`` claims.

    Sabotage proof (executed): in :meth:`EntityDensityClaimExtractor.extract`
    change the blank-input ``return []`` to ``return ["ghost claim"]``.
    Re-run: the ``real`` case fails. Restored.
    """
    extractor = factory()
    assert extractor.extract("", top_n=3) == [], name
    assert extractor.extract("   \n  \t  ", top_n=3) == [], name
    claims = extractor.extract("Alpha shipped. Beta is closed. Gamma is active. Delta must wait.", top_n=2)
    assert 1 <= len(claims) <= 2, f"{name}: {claims!r}"


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_extract_raises_when_underlying_implementation_fails(name: str, factory: Callable[[], ClaimExtractor]) -> None:
    """Content that was never decoded (raw ``bytes`` from a connector)
    must surface an exception — silent fallback to an empty list would
    mask the failure and break the contradiction pipeline's "no claims
    = skip" signal.

    Sabotage proof: in :meth:`EntityDensityClaimExtractor.extract` coerce
    ``content = str(content)`` on entry. Re-run: the ``real`` case fails
    because no exception fires. Restored.
    """
    extractor = factory()
    undecoded = cast(str, cast(Any, b"Alpha shipped. Beta is closed."))
    with pytest.raises(TypeError):
        extractor.extract(undecoded, top_n=3)
