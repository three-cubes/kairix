"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`ConversationStore`.

Five Protocol methods: ``add`` / ``add_turn`` / ``search`` / ``update``
/ ``delete``. The four ``MemoryStore``-surface methods are proved by ONE
body each, run over BOTH the production vault-paradigm
:class:`kairix.memory_stores.kairix_native.KairixNativeMemoryStore`
(injected with a :class:`tests.fakes.FakeSearchPipeline` and a
``tmp_path`` document root) and the canonical
:class:`tests.fakes.FakeConversationStore` (F43 behavioural parity):

  * ``search`` returns ``[]`` when no memory matches (callers must
    tolerate empty).
  * ``update`` raises :class:`KeyError` on unknown id (silent no-op
    would mask a programming bug).
  * ``delete`` is documented as a no-op on unknown id (the
    ``returns_empty`` outcome — no exception, no row touched).
  * ``add`` raises when the underlying backend rejects the write.

``add_turn`` has NO production implementation in ``kairix/`` yet (the
mem0 / fact-extractor ConversationStore backends are future work), so
its failure-mode probe runs over a minimal Protocol-compliant probe (the
production vault store plus a delegating ``add_turn``) and the canonical
fake.

Each test carries a "Sabotage proof:" comment describing the mutation
that proves the assertion has teeth.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from kairix.core.search.pipeline import SearchPipeline
from kairix.memory_stores.kairix_native import KairixNativeMemoryStore
from tests.fakes import FakeConversationStore, FakePaths, FakeSearchPipeline

pytestmark = pytest.mark.contract

# A factory takes ``(tmp_path, backend_writable)``; ``False`` builds a
# store whose backing storage rejects writes.
StoreFactory = Callable[[Path, bool], Any]


def _vault_kwargs(tmp_path: Path, backend_writable: bool) -> dict[str, Any]:
    root = tmp_path / "vault"
    if not backend_writable:
        # The document root is a regular file, so the memories directory
        # beneath it can never be created — every write is rejected.
        root.write_text("not a directory", encoding="utf-8")
    return {
        "pipeline": cast(SearchPipeline, FakeSearchPipeline(scripted_results=[])),
        "paths": FakePaths(document_root=root),
    }


def _real_store(tmp_path: Path, backend_writable: bool) -> Any:
    return KairixNativeMemoryStore(**_vault_kwargs(tmp_path, backend_writable))


def _fake_store(_tmp_path: Path, backend_writable: bool) -> Any:
    if backend_writable:
        return FakeConversationStore()
    return FakeConversationStore(add_raises=NotADirectoryError("memories directory unavailable"))


_IMPLEMENTATIONS: list[tuple[str, StoreFactory]] = [
    ("real", _real_store),
    ("fake", _fake_store),
]


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_search_returns_empty_when_no_memory_matches_query(name: str, factory: StoreFactory, tmp_path: Path) -> None:
    """A store with no memories relevant to the query MUST return an
    empty list — callers distinguish empty from raised exception.

    Sabotage proof (executed): in :meth:`KairixNativeMemoryStore.search`
    append a synthetic memory to ``out`` before returning. Re-run: the
    ``real`` case fails. Restored.
    """
    store = factory(tmp_path, True)
    store.add("hello world", metadata={"role": "user", "conversation_id": "conv-1"})
    assert store.search("totally unrelated topic", top_k=10) == [], name


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_update_raises_key_error_when_memory_id_unknown(name: str, factory: StoreFactory, tmp_path: Path) -> None:
    """``update`` MUST raise :class:`KeyError` on unknown id — silent
    no-op would mask the caller's wrong-id bug.

    Sabotage proof: in :meth:`KairixNativeMemoryStore.update` replace the
    ``raise KeyError(...)`` with ``return``. Re-run: the ``real`` case
    fails because no exception fires. Restored.
    """
    store = factory(tmp_path, True)
    with pytest.raises(KeyError, match="no memory"):
        store.update("does-not-exist", "new content")


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_delete_returns_empty_noop_when_id_already_absent(name: str, factory: StoreFactory, tmp_path: Path) -> None:
    """``delete`` on an absent id is a documented no-op (returns
    ``None`` cleanly) — the ``returns_empty`` outcome means callers
    can replay tombstones safely without idempotency-tracking.

    Sabotage proof: in :meth:`KairixNativeMemoryStore.delete` change
    ``path.unlink(missing_ok=True)`` to ``path.unlink()``. Re-run: the
    ``real`` case fails because the call raises. Restored.
    """
    store = factory(tmp_path, True)
    # No memories yet — delete must not raise. The Protocol returns None
    # on success; we assert no exception escapes (the documented "no-op").
    store.delete("ghost-id")
    # And the store remains empty — proves nothing was created or touched.
    assert store.search("anything", top_k=10) == [], name


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_add_raises_when_underlying_backend_rejects_write(name: str, factory: StoreFactory, tmp_path: Path) -> None:
    """A store whose ``add`` cannot persist must propagate the exception
    — silent failure here would silently drop user turns.

    Sabotage proof (executed): in :meth:`KairixNativeMemoryStore.add`
    wrap the ``mkdir`` + ``write_text`` in ``try/except OSError: pass``.
    Re-run: the ``real`` case fails because the call returns an id
    instead of raising. Restored.
    """
    store = factory(tmp_path, False)
    with pytest.raises(OSError):
        store.add("any content")


class _TurnProbeStore(KairixNativeMemoryStore):
    """Minimal Protocol-compliant ConversationStore probe: the production
    vault-paradigm store plus the ``add_turn`` the Protocol requires,
    delegating to the real ``add`` (the shape a chat-paradigm backend
    layered on the vault store takes)."""

    def add_turn(self, *, message: str, role: str, conversation_id: str, timestamp: str | None = None) -> str:
        metadata = {"role": role, "conversation_id": conversation_id, "timestamp": timestamp or "1970-01-01T00:00:00Z"}
        return self.add(message, metadata=metadata)


def _probe_turn_store(tmp_path: Path, backend_writable: bool) -> Any:
    return _TurnProbeStore(**_vault_kwargs(tmp_path, backend_writable))


_TURN_IMPLEMENTATIONS: list[tuple[str, StoreFactory]] = [
    ("probe", _probe_turn_store),
    ("fake", _fake_store),
]


@pytest.mark.parametrize("name,factory", _TURN_IMPLEMENTATIONS)
def test_add_turn_raises_when_inner_add_fails(name: str, factory: StoreFactory, tmp_path: Path) -> None:
    """``add_turn`` delegates to ``add``; when the underlying ``add``
    raises, ``add_turn`` must NOT swallow it (the chat-ingestion
    pipeline relies on the propagation to checkpoint correctly).

    No production ConversationStore implements ``add_turn`` yet, so per
    the PLA-472 ruling this runs over a minimal probe (the production
    vault store + a delegating ``add_turn``) and the canonical fake.

    Sabotage proof: in :meth:`FakeConversationStore.add_turn` wrap the
    ``return self.add(...)`` call in
    ``try: ... except Exception: return "ghost-id"``. Re-run: the
    ``fake`` case fails because the call returns a string instead of
    raising. Restored.
    """
    store = factory(tmp_path, False)
    with pytest.raises(OSError):
        store.add_turn(message="hi", role="user", conversation_id="c1")
