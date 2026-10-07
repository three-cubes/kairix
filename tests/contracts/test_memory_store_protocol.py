"""Contract: MemoryStore + ConversationStore Protocol compliance.

Phase 0.1 of the mem0-vs-kairix-uplift plan. Pins the surface that
``KairixNativeMemoryStore`` and ``Mem0MemoryStore`` both implement so
upcoming backend swaps cannot silently regress the boundary.

F43 parity: every MemoryStore test runs ONE body over the REAL
:class:`KairixNativeMemoryStore` (built through
:func:`kairix.memory_stores.make_memory_store`, writing real markdown
files under ``tmp_path``, with :class:`FakeMemoryDirSearchPipeline`
injected through the ``pipeline`` seam in place of "kairix embed +
SearchPipeline") AND the canonical :class:`FakeMemoryStore`.

Two layers of test:

1. **Protocol compliance** — ``isinstance(store, MemoryStore)`` returns
   True for both impls.
2. **Round-trip semantics** — add → search → update → search → delete
   → search proves the documented contract through the public surface.
   No internal-attribute access, no monkeypatching.

There is no production ``ConversationStore`` implementation yet (the
mem0 backend is still Phase 1), so the ``add_turn`` tests run ONE body
over a minimal in-file probe impl (:class:`_ProbeConversationStore`,
written straight from the Protocol docstring) AND the canonical
:class:`FakeConversationStore`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from kairix.core.protocols import ConversationStore, Memory, MemoryStore
from kairix.memory_stores import KAIRIX_NATIVE_BACKEND, make_memory_store
from tests.fakes import FakeConversationStore, FakeMemoryDirSearchPipeline, FakeMemoryStore, FakePaths

pytestmark = pytest.mark.contract


@pytest.fixture(params=["real", "fake"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> MemoryStore:
    """The REAL kairix-native store (tmp_path vault) or the canonical fake."""
    if request.param == "fake":
        return FakeMemoryStore()
    paths = FakePaths(document_root=tmp_path / "vault")
    return make_memory_store(KAIRIX_NATIVE_BACKEND, pipeline=FakeMemoryDirSearchPipeline(paths), paths=paths)


@dataclass(frozen=True)
class _ProbeMemory:
    id: str
    content: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)


class _ProbeConversationStore:
    """Minimal ConversationStore probe written from the Protocol docstring.

    Independent of :class:`FakeConversationStore` (own id scheme + exact
    substring matching) so the parity body pins the Protocol contract,
    not one fake's internals.
    """

    def __init__(self) -> None:
        self._rows: dict[str, _ProbeMemory] = {}

    def add(self, content: str, *, metadata: dict[str, Any] | None = None) -> str:
        mem_id = f"probe-{len(self._rows)}"
        self._rows[mem_id] = _ProbeMemory(id=mem_id, content=content, score=1.0, metadata=dict(metadata or {}))
        return mem_id

    def search(self, query: str, *, top_k: int = 10) -> list[_ProbeMemory]:
        words = query.lower().split()
        hits = [m for m in self._rows.values() if any(w in m.content.lower() for w in words)]
        return hits[:top_k]

    def update(self, memory_id: str, content: str) -> None:
        old = self._rows[memory_id]
        self._rows[memory_id] = _ProbeMemory(id=old.id, content=content, score=old.score, metadata=old.metadata)

    def delete(self, memory_id: str) -> None:
        self._rows.pop(memory_id, None)

    def add_turn(self, *, message: str, role: str, conversation_id: str, timestamp: str | None = None) -> str:
        md = {"role": role, "conversation_id": conversation_id, "timestamp": timestamp or "1970-01-01T00:00:00Z"}
        return self.add(message, metadata=md)


@pytest.fixture(params=["probe", "fake"])
def conv_store(request: pytest.FixtureRequest) -> Any:
    """A ConversationStore — the minimal Protocol probe or the canonical fake."""
    if request.param == "fake":
        return FakeConversationStore()
    return _ProbeConversationStore()


# ---------------------------------------------------------------------------
# Protocol compliance — sabotage-proven via isinstance() at runtime
# ---------------------------------------------------------------------------


def test_memory_store_satisfies_memory_store_protocol(store: MemoryStore) -> None:
    """Real + fake both satisfy MemoryStore via runtime isinstance().

    Sabotage-proof: rename ``KairixNativeMemoryStore.delete`` in
    kairix/memory_stores/kairix_native.py — the runtime_checkable probe
    for all four methods fails on the real leg.
    """
    assert isinstance(store, MemoryStore)


def test_fake_conversation_store_satisfies_both_protocols(conv_store: Any) -> None:
    """Probe + FakeConversationStore satisfy BOTH ConversationStore AND MemoryStore.

    Sabotage-proof: remove ``add_turn`` from FakeConversationStore and
    only the ConversationStore assertion fails (fake leg); remove ``add``
    and both fail.
    """
    conv = conv_store
    assert isinstance(conv, MemoryStore), "ConversationStore must include the MemoryStore surface"
    assert isinstance(conv, ConversationStore), "ConversationStore must include add_turn"


def test_search_returns_memory_protocol_objects(store: MemoryStore) -> None:
    """``MemoryStore.search`` returns objects satisfying the Memory Protocol.

    Sabotage-proof: drop the ``score=`` field from ``KairixNativeMemory``
    (kairix/memory_stores/kairix_native.py) — construction / the
    runtime ``Memory`` probe fails on the real leg.
    """
    store.add("alpha beta gamma", metadata={"source": "test"})
    results = store.search("alpha", top_k=5)
    assert results, "search must surface a memory whose content shares words with the query"
    for m in results:
        assert isinstance(m, Memory), (
            f"every search result must satisfy Memory Protocol; got {type(m).__name__}. "
            f"fix: add id/content/score/metadata properties to the result type"
        )


# ---------------------------------------------------------------------------
# Round-trip semantics — the documented contract
# ---------------------------------------------------------------------------


def test_add_returns_id_and_round_trips_through_search(store: MemoryStore) -> None:
    """``add`` returns a non-empty id; subsequent ``search`` surfaces it.

    Sabotage-proof: make ``KairixNativeMemoryStore.add`` skip the
    ``path.write_text`` — the real leg's search finds nothing.
    """
    mem_id = store.add("the quick brown fox", metadata={"source": "test"})
    assert isinstance(mem_id, str) and mem_id, "add must return a non-empty string id"
    results = store.search("quick", top_k=5)
    matched = [m for m in results if m.id == mem_id]
    assert matched, f"search('quick') must surface id={mem_id!r}; got ids={[m.id for m in results]}"
    assert "the quick brown fox" in matched[0].content


def test_update_replaces_content_and_search_reflects_change(store: MemoryStore) -> None:
    """``update`` replaces content; the new content is what ``search`` returns.

    Sabotage-proof: make ``KairixNativeMemoryStore.update`` write
    ``existing`` content instead of ``content`` — the real leg fails.
    """
    mem_id = store.add("original content")
    store.update(mem_id, "completely different replacement")
    matched = [m for m in store.search("replacement", top_k=5) if m.id == mem_id]
    assert matched, "search must surface the memory under the new content's terms"
    assert matched[0].content == "completely different replacement"
    # And the old content is no longer findable for this id
    old_match = [m for m in store.search("original", top_k=5) if m.id == mem_id]
    assert not old_match, "old content must not be searchable after update"


def test_update_missing_id_raises_key_error(store: MemoryStore) -> None:
    """``update`` on a non-existent id raises KeyError per the Protocol docstring.

    Sabotage-proof: drop the ``raise KeyError`` guard in
    ``KairixNativeMemoryStore.update`` — the real leg sees no raise.
    """
    with pytest.raises(KeyError, match="no memory with id"):
        store.update("not-a-real-id", "new content")


def test_delete_removes_memory_and_search_no_longer_surfaces_it(store: MemoryStore) -> None:
    """``delete`` removes the memory; subsequent ``search`` excludes it.

    Sabotage-proof: make ``KairixNativeMemoryStore.delete`` a no-op —
    the real leg still surfaces the deleted memory.
    """
    mem_id = store.add("ephemeral content here")
    store.delete(mem_id)
    results = store.search("ephemeral", top_k=5)
    assert not [m for m in results if m.id == mem_id], "deleted memory must not surface in search"


def test_delete_missing_id_is_noop(store: MemoryStore) -> None:
    """``delete`` of a non-existent id is a no-op per the Protocol docstring.

    Sabotage-proof: change ``missing_ok=True`` to ``False`` in
    ``KairixNativeMemoryStore.delete`` — the real leg raises.
    """
    # Must not raise
    store.delete("not-a-real-id")
    # And other state is untouched
    mem_id = store.add("still here")
    assert store.search("still", top_k=5), "delete-noop must not corrupt other state"
    assert mem_id


def test_search_empty_store_returns_empty_list(store: MemoryStore) -> None:
    """``search`` on an empty store returns ``[]``, not None, not raises.

    Sabotage-proof: make ``KairixNativeMemoryStore.search`` return
    ``None`` when there are no hits — the real leg fails ``== []``.
    """
    result = store.search("anything", top_k=10)
    assert result == [], f"empty store must return []; got {result!r}"


def test_search_top_k_caps_result_size(store: MemoryStore) -> None:
    """``search`` returns at most ``top_k`` memories.

    Sabotage-proof: drop the ``[:top_k]`` slice in
    ``KairixNativeMemoryStore.search`` — the real leg returns 20.
    """
    for i in range(20):
        store.add(f"common term entry number {i}")
    result = store.search("common", top_k=3)
    assert len(result) == 3, f"top_k=3 must return at most 3 memories; got {len(result)}"


def test_search_returns_best_first_by_score(store: MemoryStore) -> None:
    """``search`` returns memories sorted by score, best first.

    Sabotage-proof: make ``KairixNativeMemoryStore.search`` iterate
    ``reversed(result.results)`` — the real leg returns worst first.
    """
    store.add("alpha beta gamma delta epsilon")  # 5 words; query "alpha beta" → 2/2 overlap
    store.add("alpha zeta eta theta iota kappa")  # 6 words; query "alpha beta" → 1/2 overlap
    result = store.search("alpha beta", top_k=2)
    assert len(result) == 2
    assert result[0].score >= result[1].score, (
        f"results must be sorted by score descending; got {result[0].score} then {result[1].score}"
    )


# ---------------------------------------------------------------------------
# ConversationStore — turn-level ingestion round-trips into the search surface
# ---------------------------------------------------------------------------


def test_add_turn_metadata_round_trips_through_search(conv_store: Any) -> None:
    """``add_turn``'s role/conversation_id/timestamp survive into ``search`` metadata.

    Sabotage-proof: change ``add_turn`` to drop ``conversation_id`` from
    the metadata dict; this assertion fails because the round-trip loses
    the field.
    """
    conv = conv_store
    mem_id = conv.add_turn(
        message="The benchmark expects this exact phrase",
        role="user",
        conversation_id="conv-26",
        timestamp="2026-05-20T07:00:00Z",
    )
    results = conv.search("benchmark", top_k=5)
    matched = [m for m in results if m.id == mem_id]
    assert matched, "search must find the just-added turn"
    md = matched[0].metadata
    assert md["role"] == "user", f"role lost in round-trip; got {md!r}"
    assert md["conversation_id"] == "conv-26", f"conversation_id lost in round-trip; got {md!r}"
    assert md["timestamp"] == "2026-05-20T07:00:00Z", f"timestamp lost in round-trip; got {md!r}"


def test_add_turn_assigns_default_timestamp_when_omitted(conv_store: Any) -> None:
    """``add_turn`` without ``timestamp`` stamps a default rather than ``None``.

    Backends with proper now-stamping behaviour will produce a real ISO-8601
    string; the FakeConversationStore uses an epoch sentinel so the test is
    deterministic. Either way, metadata['timestamp'] must be a string.
    """
    conv = conv_store
    mem_id = conv.add_turn(message="no timestamp", role="user", conversation_id="conv-x")
    matched = [m for m in conv.search("timestamp", top_k=5) if m.id == mem_id]
    assert matched
    assert isinstance(matched[0].metadata["timestamp"], str)
    assert matched[0].metadata["timestamp"], "timestamp must be a non-empty string"
