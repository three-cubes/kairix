"""Contract + integration tests for :class:`KairixNativeMemoryStore`.

Phase 0.2 of the mem0-vs-kairix-uplift plan. Pins the adapter's
behaviour through its public surface:

- Protocol compliance: ``isinstance(store, MemoryStore)`` returns True
- add → file written under document_root/memories with frontmatter+body
- search → delegates to injected SearchPipeline, maps hits → Memory
- update → rewrites file body, preserves frontmatter
- delete → removes file; no-op if absent

DI is clean per F1: tests construct a fake ``SearchPipeline`` (or
``_StubPipeline`` for the search-only path), pass it via the
``pipeline=`` kwarg. No monkeypatch, no internal-attribute reassignment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from kairix.core.protocols import Memory, MemoryStore
from kairix.core.search.budget import BudgetedResult, Tier
from kairix.core.search.rrf import FusedResult
from kairix.memory_stores.kairix_native import KairixNativeMemory, KairixNativeMemoryStore
from kairix.paths import KairixPaths
from tests.fakes import FakePaths

pytestmark = pytest.mark.contract


# ---------------------------------------------------------------------------
# Stub SearchPipeline — just enough to drive search() without standing up the
# real pipeline. The real pipeline is exercised by the existing pipeline
# integration tests; this adapter test pins the wrapper shape only.
# ---------------------------------------------------------------------------


def _stub_hit(
    path: str,
    title: str,
    snippet: str,
    score: float,
    tier: Tier = "L0",
    tokens: int = 0,
    collection: str = "shared",
) -> BudgetedResult:
    """Build a hit in the REAL SearchPipeline shape (``BudgetedResult``).

    The pipeline returns ``BudgetedResult`` rows wrapping a ``FusedResult``
    (path/title/collection/score on ``.result``, text on ``.content``) —
    the old flat stub encoded a shape the real pipeline never produces.
    """
    fused = FusedResult(path=path, collection=collection, title=title, snippet=snippet, boosted_score=score)
    return BudgetedResult(result=fused, tier=tier, token_estimate=tokens, content=snippet)


@dataclass
class _StubSearchResult:
    """Stand-in for kairix.core.search.pipeline.SearchResult."""

    results: list[BudgetedResult]
    query: str = ""
    intent: Any = None
    bm25_count: int = 0
    vec_count: int = 0
    fused_count: int = 0
    error: str = ""


class _StubPipeline:
    """Records every search() call; returns a configured list of hits."""

    def __init__(self, hits: list[BudgetedResult] | None = None) -> None:
        self._hits = list(hits or [])
        self.calls: list[dict[str, Any]] = []

    def search(self, **kwargs: Any) -> _StubSearchResult:
        self.calls.append(dict(kwargs))
        return _StubSearchResult(results=list(self._hits))


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


def test_kairix_native_memory_store_satisfies_memory_store_protocol(tmp_path) -> None:
    """``isinstance(store, MemoryStore)`` returns True via runtime probe.

    Sabotage-proof: rename ``KairixNativeMemoryStore.delete`` to ``destroy``
    and the runtime_checkable probe fails because ``delete`` is missing.
    """
    paths: KairixPaths = FakePaths(document_root=tmp_path)
    store = KairixNativeMemoryStore(pipeline=_StubPipeline(), paths=paths)
    assert isinstance(store, MemoryStore), (
        "KairixNativeMemoryStore must satisfy MemoryStore Protocol via runtime isinstance check"
    )


def test_kairix_native_memory_satisfies_memory_protocol() -> None:
    """A ``KairixNativeMemory`` instance satisfies the Memory Protocol."""
    mem = KairixNativeMemory(id="abc", content="hello", score=0.5, metadata={"k": "v"})
    assert isinstance(mem, Memory), "KairixNativeMemory must satisfy Memory Protocol"


# ---------------------------------------------------------------------------
# add → filesystem
# ---------------------------------------------------------------------------


def test_add_writes_markdown_file_under_memories_subdir(tmp_path) -> None:
    """``add`` writes a markdown file under ``document_root/memories/``."""
    paths = FakePaths(document_root=tmp_path)
    store = KairixNativeMemoryStore(pipeline=_StubPipeline(), paths=paths)
    mem_id = store.add("alpha beta gamma", metadata={"source": "test", "agent": "agent-alpha"})

    expected = tmp_path / "memories" / f"{mem_id}.md"
    assert expected.exists(), f"expected file at {expected}, got dir contents: {list(tmp_path.rglob('*'))}"
    body = expected.read_text(encoding="utf-8")
    assert "alpha beta gamma" in body
    assert "source: test" in body
    assert "agent: agent-alpha" in body


def test_add_returns_deterministic_id_for_same_content(tmp_path) -> None:
    """Same content → same id (content-hash determinism).

    Sabotage-proof: change ``_content_hash_id`` to use a random uuid and
    this assertion fails because two adds of the same content produce
    different ids.
    """
    paths = FakePaths(document_root=tmp_path)
    store = KairixNativeMemoryStore(pipeline=_StubPipeline(), paths=paths)
    id1 = store.add("same content")
    id2 = store.add("same content")
    assert id1 == id2, f"deterministic ids required; got {id1!r} vs {id2!r}"


def test_add_with_no_metadata_writes_content_only(tmp_path) -> None:
    """No metadata → no frontmatter block in the rendered markdown."""
    paths = FakePaths(document_root=tmp_path)
    store = KairixNativeMemoryStore(pipeline=_StubPipeline(), paths=paths)
    mem_id = store.add("plain body")
    body = (tmp_path / "memories" / f"{mem_id}.md").read_text(encoding="utf-8")
    assert "---" not in body, f"expected no frontmatter for metadata-less add, got: {body!r}"
    assert "plain body" in body


# ---------------------------------------------------------------------------
# search → pipeline delegation
# ---------------------------------------------------------------------------


def test_search_delegates_to_pipeline_with_query(tmp_path) -> None:
    """``search`` calls the injected pipeline's ``search`` with the query."""
    paths = FakePaths(document_root=tmp_path)
    pipeline = _StubPipeline(hits=[])
    store = KairixNativeMemoryStore(pipeline=pipeline, paths=paths)
    store.search("test query")
    assert len(pipeline.calls) == 1
    assert pipeline.calls[0]["query"] == "test query"


def test_search_maps_hits_to_memory_objects(tmp_path) -> None:
    """Pipeline hits map cleanly into ``KairixNativeMemory`` records."""
    paths = FakePaths(document_root=tmp_path)
    hits = [
        _stub_hit(path="memories/b.md", title="Doc B", snippet="snippet b", score=0.9),
        _stub_hit(path="memories/d.md", title="Doc D", snippet="snippet d", score=0.6),
    ]
    store = KairixNativeMemoryStore(pipeline=_StubPipeline(hits=hits), paths=paths)
    result = store.search("anything")
    assert len(result) == 2
    assert result[0].id == "b", "id is the file stem — the same id add() returns"
    assert result[0].metadata["path"] == "memories/b.md"
    assert result[0].content == "snippet b"
    assert result[0].score == 0.9
    assert result[0].metadata["title"] == "Doc B"
    assert result[0].metadata["collection"] == "shared"


def test_search_projects_tier_content_tier_and_token_count(tmp_path) -> None:
    """A hit's tier-selected ``content`` (not the fused snippet), its score,
    tier and token estimate all surface on the Memory.

    Sabotage proof: in ``KairixNativeMemoryStore._hit_to_memory`` change any
    ``or`` default on the content / score / path / title / collection / tier /
    tokens projections to ``and`` -- a populated field collapses to its
    empty default (or to the fallback snippet) and an assertion here fails.
    """
    paths = FakePaths(document_root=tmp_path)
    fused = FusedResult(
        path="memories/c.md", collection="shared", title="Doc C", snippet="fused snippet", boosted_score=0.7
    )
    hit = BudgetedResult(result=fused, tier="L1", token_estimate=42, content="tier selected text")
    store = KairixNativeMemoryStore(pipeline=_StubPipeline(hits=[hit]), paths=paths)

    [mem] = store.search("anything")

    assert mem.id == "c"
    assert mem.content == "tier selected text", "tier content wins over the fused snippet"
    assert mem.score == 0.7
    assert mem.metadata == {
        "path": "memories/c.md",
        "title": "Doc C",
        "collection": "shared",
        "tier": "L1",
        "tokens": 42,
    }


def test_search_falls_back_to_snippet_when_tier_content_is_empty(tmp_path) -> None:
    """An empty tier ``content`` falls back to the fused snippet text.

    Sabotage proof: in ``_hit_to_memory`` change either ``or`` on the
    content projection to ``and`` -- the memory content comes back empty.
    """
    paths = FakePaths(document_root=tmp_path)
    fused = FusedResult(path="memories/e.md", collection="shared", title="Doc E", snippet="fallback snippet")
    hit = BudgetedResult(result=fused, tier="L0", token_estimate=3, content="")
    store = KairixNativeMemoryStore(pipeline=_StubPipeline(hits=[hit]), paths=paths)

    [mem] = store.search("anything")

    assert mem.content == "fallback snippet"


def test_search_caps_results_at_top_k(tmp_path) -> None:
    """``top_k`` truncates the pipeline's result list."""
    paths = FakePaths(document_root=tmp_path)
    hits = [_stub_hit(path=f"/x/{i}.md", title=f"t{i}", snippet=f"s{i}", score=1.0 - i * 0.1) for i in range(10)]
    store = KairixNativeMemoryStore(pipeline=_StubPipeline(hits=hits), paths=paths)
    result = store.search("anything", top_k=3)
    assert len(result) == 3, f"top_k=3 must cap result count; got {len(result)}"


def test_search_empty_pipeline_result_returns_empty_list(tmp_path) -> None:
    """Pipeline returning no hits → search returns ``[]`` (no exception)."""
    paths = FakePaths(document_root=tmp_path)
    store = KairixNativeMemoryStore(pipeline=_StubPipeline(hits=[]), paths=paths)
    result = store.search("nothing matches")
    assert result == []


# ---------------------------------------------------------------------------
# update / delete — filesystem mutations
# ---------------------------------------------------------------------------


def test_update_rewrites_body_preserving_frontmatter(tmp_path) -> None:
    """``update`` rewrites the content; frontmatter survives."""
    paths = FakePaths(document_root=tmp_path)
    store = KairixNativeMemoryStore(pipeline=_StubPipeline(), paths=paths)
    mem_id = store.add("original body", metadata={"source": "test"})
    store.update(mem_id, "replaced body")

    body = (tmp_path / "memories" / f"{mem_id}.md").read_text(encoding="utf-8")
    assert "replaced body" in body, f"new content missing; got: {body!r}"
    assert "original body" not in body, "old content must be replaced"
    assert "source: test" in body, "frontmatter must survive the update"


def test_update_missing_id_raises_key_error(tmp_path) -> None:
    """``update`` on a non-existent id raises ``KeyError`` per the contract."""
    paths = FakePaths(document_root=tmp_path)
    store = KairixNativeMemoryStore(pipeline=_StubPipeline(), paths=paths)
    with pytest.raises(KeyError, match="no memory with id"):
        store.update("does-not-exist", "new content")


def test_delete_removes_file(tmp_path) -> None:
    """``delete`` removes the markdown file."""
    paths = FakePaths(document_root=tmp_path)
    store = KairixNativeMemoryStore(pipeline=_StubPipeline(), paths=paths)
    mem_id = store.add("delete me", metadata={"source": "test"})
    path = tmp_path / "memories" / f"{mem_id}.md"
    assert path.exists()
    store.delete(mem_id)
    assert not path.exists(), "file must be removed after delete"


def test_delete_missing_id_is_noop(tmp_path) -> None:
    """``delete`` of a non-existent id does not raise."""
    paths = FakePaths(document_root=tmp_path)
    store = KairixNativeMemoryStore(pipeline=_StubPipeline(), paths=paths)
    store.delete("does-not-exist")  # must not raise
