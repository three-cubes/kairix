"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`MemoryStore`.

Four methods on the memory-backend surface (``add``, ``search``,
``update``, ``delete``). Per the Protocol docstring:

  * ``search`` returns ``[]`` for no matches (callers MUST tolerate it
    — empty is a valid "no relevant content" signal).
  * ``update`` raises KeyError-equivalent on missing id.
  * ``delete`` is a no-op on missing id.

The ``add`` failure shape is "raises on backend write failure".

F43 parity: every test runs ONE body over the REAL
:class:`KairixNativeMemoryStore` (built via ``make_memory_store`` over a
``tmp_path`` vault, :class:`FakeMemoryDirSearchPipeline` injected through
the ``pipeline`` seam) AND the canonical :class:`FakeMemoryStore`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kairix.core.protocols import MemoryStore
from kairix.memory_stores import KAIRIX_NATIVE_BACKEND, make_memory_store
from tests.fakes import FakeMemoryDirSearchPipeline, FakeMemoryStore, FakePaths

pytestmark = pytest.mark.contract


def _real_store(document_root: Path) -> MemoryStore:
    paths = FakePaths(document_root=document_root)
    return make_memory_store(KAIRIX_NATIVE_BACKEND, pipeline=FakeMemoryDirSearchPipeline(paths), paths=paths)


@pytest.fixture(params=["real", "fake"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> MemoryStore:
    """A healthy store — the REAL kairix-native adapter or the canonical fake."""
    if request.param == "fake":
        return FakeMemoryStore()
    return _real_store(tmp_path / "vault")


@pytest.fixture(params=["real", "fake"])
def unwritable_store(request: pytest.FixtureRequest, tmp_path: Path) -> MemoryStore:
    """A store whose backend write fails.

    Real: ``document_root`` is a regular FILE, so creating
    ``<document_root>/memories/`` raises an ``OSError`` subclass.
    Fake: the ``add_raises`` knob injects the same ``OSError`` family.
    """
    if request.param == "fake":
        return FakeMemoryStore(add_raises=NotADirectoryError("F68-add-raises: memories dir not writable"))
    blocker = tmp_path / "vault-is-a-file"
    blocker.write_text("not a directory")
    return _real_store(blocker)


def test_add_raises_propagates_typed_exception(unwritable_store: MemoryStore) -> None:
    """``add`` surfacing a backend write failure must raise — caller
    must not interpret a swallowed error as "id assigned".

    Sabotage proof: wrap the body of ``KairixNativeMemoryStore.add``
    (kairix/memory_stores/kairix_native.py) in ``try/except OSError:
    return mem_id``. Re-run: the real leg's pytest.raises sees nothing.
    Restored.
    """
    with pytest.raises(OSError):
        unwritable_store.add("anything", metadata={})


def test_search_returns_empty_when_store_is_empty(store: MemoryStore) -> None:
    """``search`` on an empty store returns ``[]`` — not None, not
    raises. Callers iterate without a null check.

    Sabotage proof: in ``KairixNativeMemoryStore.search`` return
    ``None`` when ``out`` is empty. Re-run: the real leg's ``== []``
    assertion fails. Restored.
    """
    out = store.search("any query", top_k=10)
    assert out == [], f"empty store must yield []; got {out!r}"


def test_update_raises_when_id_missing(store: MemoryStore) -> None:
    """``update`` on a non-existent id raises KeyError per the
    Protocol docstring — the failure surface for "id absent".

    Sabotage proof: in ``KairixNativeMemoryStore.update`` change
    ``raise KeyError(...)`` to ``return``. Re-run: the real leg's
    pytest.raises sees nothing. Restored.
    """
    with pytest.raises(KeyError, match="no memory with id"):
        store.update("never-added-id", "new content")


def test_delete_returns_empty_when_id_missing(store: MemoryStore) -> None:
    """``delete`` of a non-existent id is a no-op (idempotent) per the
    Protocol docstring. Observable: state digest unchanged.

    Sabotage proof: in ``KairixNativeMemoryStore.delete`` change
    ``unlink(missing_ok=True)`` to ``unlink()``. Re-run: the real leg
    raises FileNotFoundError and the test fails. Restored.
    """
    pre_id = store.add("existing content")
    # delete a non-existent id must absorb cleanly
    store.delete("never-added-id")
    # state unchanged: pre-existing entry still surfaces under search
    assert store.search("existing", top_k=5), (
        "delete of an absent id must not corrupt other state; pre-existing entry must still be searchable"
    )
    assert pre_id
