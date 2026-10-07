"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`EmbeddingService`.

Two Protocol methods: ``embed(text)`` + ``embed_batch(texts)``.

Every body runs over BOTH the production
:class:`kairix.transport.embed_service.ProviderEmbeddingService`
(wrapping a :class:`tests.fakes.FakeProvider` plugin, with an isolated
in-memory :class:`EmbedCache` and no coalescer injected through its
constructor seams) and the canonical
:class:`tests.fakes.FakeEmbeddingService` (F43 behavioural parity).

``returns_empty`` is THE failure signal of this Protocol: an empty
vector (``[]``, or ``[]`` per input for a batch) tells callers "embed
failed, skip the vector path".

Parity finding (PLA-472): the previous contract asserted that ``embed``
/ ``embed_batch`` RAISE when the provider crashes, proved only against
inline raising stubs. The production adapter is documented and built as
never-raises — a provider crash surfaces as ``[]`` (single) / ``[[]...]``
(batch). The contract now pins that real observable; the fake's
equivalent is its ``vector=[]`` soft-failure mode. (The fake's
``raises=`` knob models a raising embedder production never produces
through this adapter.)

Each test carries a "Sabotage proof:" comment describing the mutation
that proves the assertion has teeth.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from kairix.core.protocols import EmbeddingService
from kairix.transport.cache import EmbedCache
from kairix.transport.embed_service import ProviderEmbeddingService
from tests.fakes import FakeEmbeddingService, FakeProvider

pytestmark = pytest.mark.contract

# A factory takes the failure mode: "empty" (the provider returns empty
# vectors) or "crash" (the provider raises).
ServiceFactory = Callable[[str], EmbeddingService]


def _real_service(failure: str) -> EmbeddingService:
    provider = (
        FakeProvider(embed_empty=True)
        if failure == "empty"
        else FakeProvider(embed_raises=RuntimeError("F68-embed-provider-down"))
    )
    cache = EmbedCache()
    return ProviderEmbeddingService(
        provider,
        existing_coalescer_fn=lambda: None,
        coalescer_factory=lambda **_: None,
        get_embed_cache_fn=lambda: cache,
    )


def _fake_service(_failure: str) -> EmbeddingService:
    return FakeEmbeddingService(vector=[])


_IMPLEMENTATIONS: list[tuple[str, ServiceFactory]] = [
    ("real", _real_service),
    ("fake", _fake_service),
]


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_embed_returns_empty_when_configured_with_empty_vector(name: str, factory: ServiceFactory) -> None:
    """When the embedder yields an empty vector, ``embed`` returns ``[]``
    — the documented short-circuit signal backends use to mark "embed
    failed, skip vector path".

    Sabotage proof: in ``ProviderEmbeddingService.embed`` change
    ``if not vectors or not vectors[0]: return []`` to ``return [0.0]``.
    Re-run: the ``real`` case fails. Restored.
    """
    assert factory("empty").embed("any text") == [], f"{name}: empty-vector service must return []"


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_embed_batch_returns_empty_vectors_per_input_when_configured_empty(name: str, factory: ServiceFactory) -> None:
    """``embed_batch`` over an empty-vector embedder yields one ``[]``
    per input — order + length preserved so callers can still align
    vectors with their inputs (just every slot is empty).

    Sabotage proof: in ``FakeProvider.embed_batch`` return
    ``[[]]`` regardless of input length. Re-run: the ``real`` case fails
    because the result has length 1. Restored.
    """
    out = factory("empty").embed_batch(["a", "b", "c"])
    assert out == [[], [], []], f"{name}: empty-configured batch must preserve length; got {out!r}"


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_embed_returns_empty_when_underlying_provider_crashes(name: str, factory: ServiceFactory) -> None:
    """A provider crash surfaces as ``[]`` from ``embed`` — never an
    unhandled exception out of the search pipeline (see the PLA-472
    finding in the module docstring).

    Sabotage proof (executed): in ``ProviderEmbeddingService.embed``
    re-raise from the ``except Exception`` branch. Re-run: the ``real``
    case fails with the provider's RuntimeError. Restored.
    """
    assert factory("crash").embed("any") == [], name


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_embed_batch_returns_empty_when_underlying_provider_crashes(name: str, factory: ServiceFactory) -> None:
    """Mirrors ``embed`` — a provider crash in ``embed_batch`` yields one
    ``[]`` per input so the caller can attribute the failure per text.

    Sabotage proof (executed): in ``ProviderEmbeddingService.embed_batch``
    change the ``except`` branch to ``return []``. Re-run: the ``real``
    case fails because the per-input alignment is lost. Restored.
    """
    assert factory("crash").embed_batch(["a", "b"]) == [[], []], name
