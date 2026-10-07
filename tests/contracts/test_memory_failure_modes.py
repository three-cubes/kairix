"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`Memory`.

Four read-only properties on the recalled-memory value object
(``id``, ``content``, ``score``, ``metadata``). The Protocol explicitly
uses ``@property`` descriptors — they're part of the Protocol's
behavioural surface.

Failure surface:

  * ``id`` / ``score`` — raise ``AttributeError`` on reassignment: a
    recalled Memory is an immutable value object, so a caller that tries
    to rewrite its identity or rescale its score in place fails loudly
    instead of silently corrupting a shared recall result.
  * ``content`` — returns_empty when the memory has been tombstoned /
    superseded — an empty string is still a valid Memory shape.
  * ``metadata`` — returns_empty when the backend produced no metadata
    (the documented default).

F43 parity: every body runs over BOTH the real
:class:`kairix.memory_stores.kairix_native.KairixNativeMemory` (the
frozen dataclass ``KairixNativeMemoryStore.search`` returns) AND the
canonical :class:`tests.fakes.FakeMemory`.

Finding (fake-vs-real drift): the previous ``raises`` tests ran only an
inline ``_BrokenMemory`` whose ``id`` / ``score`` getters raised on read.
No production Memory can do that — ``KairixNativeMemory`` is a frozen
dataclass whose fields always read. The shared, genuine raising contract
is immutability, pinned below.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from kairix.core.protocols import Memory
from kairix.memory_stores.kairix_native import KairixNativeMemory
from tests.fakes import FakeMemory

pytestmark = pytest.mark.contract

_IDS = ["real", "fake"]

_MemoryFactory = Callable[..., Memory]
_IMPLS: list[_MemoryFactory] = [KairixNativeMemory, FakeMemory]


@pytest.mark.parametrize("factory", _IMPLS, ids=_IDS)
def test_id_raises_attribute_error_on_reassignment(factory: _MemoryFactory) -> None:
    """``id`` is the backend-assigned identity — rewriting it on a recalled
    Memory raises instead of silently re-pointing ``update`` / ``delete``.

    Sabotage proof: change ``@dataclass(frozen=True)`` on
    ``KairixNativeMemory`` to ``@dataclass``. Re-ran: the real leg's
    assignment succeeds and ``pytest.raises`` sees nothing. Restored.
    """
    mem = factory(id="m1", content="text", score=0.5)
    assert isinstance(mem, Memory)
    target: Any = mem
    with pytest.raises(AttributeError):
        target.id = "rewritten"
    assert mem.id == "m1"


@pytest.mark.parametrize("factory", _IMPLS, ids=_IDS)
def test_content_returns_empty_when_memory_is_tombstoned(factory: _MemoryFactory) -> None:
    """``content`` may be an empty string when the memory has been
    tombstoned / superseded — an empty string is still a valid Memory
    shape per the Protocol; callers tolerate it.

    Sabotage proof: add ``def __post_init__(self): object.__setattr__(self,
    "content", self.content or "[empty]")`` to ``KairixNativeMemory``.
    Re-ran: the real leg's ``== ""`` assertion fails. Restored.
    """
    mem = factory(id="m1", content="", score=0.5)
    assert mem.content == "", f"empty-content tombstone must round-trip as ''; got {mem.content!r}"


@pytest.mark.parametrize("factory", _IMPLS, ids=_IDS)
def test_score_raises_attribute_error_on_reassignment(factory: _MemoryFactory) -> None:
    """``score`` is rescaled to [0.0, 1.0] by the backend once — a caller
    re-scoring a shared recall result in place raises instead of
    silently skewing cross-backend fusion.

    Sabotage proof: change ``@dataclass(frozen=True)`` on
    ``KairixNativeMemory`` to ``@dataclass``. Re-ran: the real leg's
    assignment succeeds and ``pytest.raises`` sees nothing. Restored.
    """
    mem = factory(id="m1", content="text", score=0.25)
    target: Any = mem
    with pytest.raises(AttributeError):
        target.score = 1.0
    assert mem.score == 0.25


@pytest.mark.parametrize("factory", _IMPLS, ids=_IDS)
def test_metadata_returns_empty_when_backend_produced_no_metadata(factory: _MemoryFactory) -> None:
    """``metadata`` returns an empty dict when the backend produced
    none — callers iterate without a None check.

    Sabotage proof: change ``KairixNativeMemory.metadata``'s default to
    ``field(default=None)``. Re-ran: the real leg's ``== {}`` assertion
    fails because metadata is ``None``. Restored.
    """
    mem = factory(id="m1", content="text", score=0.5)
    assert mem.metadata == {}, f"absent-metadata default must be {{}}; got {mem.metadata!r}"
