"""``KairixNativeMemoryStore`` — vault-paradigm MemoryStore adapter.

Wraps the existing :class:`kairix.core.search.pipeline.SearchPipeline` and
:class:`kairix.paths.KairixPaths` to satisfy the
:class:`kairix.core.protocols.MemoryStore` Protocol.

Vault-paradigm semantics: memories are markdown files under
``paths.document_root / "memories" /``. ``add`` writes the file;
``update`` rewrites it; ``delete`` removes it. The caller is responsible
for running ``kairix embed`` (or the equivalent ingest path) to make
new memories findable by ``search`` — this adapter does not couple the
filesystem layer to the index-build layer.

This is the adapter the LoCoMo benchmark spike will use as the
``--backend kairix-native`` option (Phase 0.3). The mem0 alternative
lands at Phase 1 under ``kairix/memory_stores/mem0_backend.py``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from kairix.core.search.pipeline import SearchPipeline
from kairix.paths import KairixPaths

#: Subdirectory under ``document_root`` where ``add``/``update``/``delete``
#: write markdown files. Vault paradigm — kept separate from operator-curated
#: directories (``00-Home/``, ``01-Projects/``, etc.) so ingest separators
#: stay clean.
_MEMORIES_SUBDIR = "memories"

#: Length of the content-hash id. SHA-256 truncated to 16 hex chars =
#: 64 bits of entropy; collision-free at any plausible single-process
#: memory count. Matches the id shape used by ``kairix embed`` for
#: vault-derived chunks.
_ID_PREFIX_LEN = 16


@dataclass(frozen=True)
class KairixNativeMemory:
    """Memory returned by :meth:`KairixNativeMemoryStore.search`.

    Satisfies the :class:`kairix.core.protocols.Memory` Protocol —
    runtime ``isinstance(m, Memory)`` returns ``True`` because the
    Protocol's runtime probe checks attribute existence (``id``,
    ``content``, ``score``, ``metadata``) and a frozen dataclass
    surfaces those as attributes.
    """

    id: str
    content: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)


class KairixNativeMemoryStore:
    """MemoryStore Protocol adapter for the kairix-native (vault) backend.

    Construction is DI-clean: both ``pipeline`` and ``paths`` are
    injected explicitly. Tests pass a fake pipeline (via
    :func:`build_search_pipeline` with a ``FakeProviderRegistry`` or a
    direct stub) and a sentinel :class:`KairixPaths`; production callers
    wire the real ``build_search_pipeline()`` output and
    ``KairixPaths.resolve()``.

    Operation semantics:

    - ``add(content, metadata)``: writes a markdown file to
      ``paths.document_root / "memories" / <id>.md`` with a YAML
      frontmatter carrying ``metadata``. Returns the id. Does NOT
      re-index — the caller runs ``kairix embed`` for that.
    - ``search(query, top_k)``: delegates to ``pipeline.search`` and
      maps each ``BudgetedResult`` row into a :class:`KairixNativeMemory`
      whose ``id`` is the same id ``add`` returned (the file stem), so
      it round-trips into ``update``/``delete``. Empty list on "no
      relevant content".
    - ``update(memory_id, content)``: rewrites the file body (preserves
      frontmatter). Raises ``KeyError`` if the file does not exist.
    - ``delete(memory_id)``: removes the file. No-op if absent.
    """

    def __init__(self, *, pipeline: SearchPipeline, paths: KairixPaths) -> None:
        self._pipeline = pipeline
        self._paths = paths

    # ------------------------------------------------------------------
    # MemoryStore Protocol surface
    # ------------------------------------------------------------------

    def add(self, content: str, *, metadata: dict[str, Any] | None = None) -> str:
        """Write a memory as a markdown file; return its content-hash id."""
        md = dict(metadata or {})
        mem_id = self._content_hash_id(content)
        path = self._memory_path(mem_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self._render_markdown(md, content), encoding="utf-8")
        return mem_id

    def search(self, query: str, *, top_k: int = 10) -> list[KairixNativeMemory]:
        """Delegate to :meth:`SearchPipeline.search`; map hits → memories.

        ``top_k`` truncates the pipeline's result list — the pipeline's
        own budget/limit is separate (token budget, not hit count), so
        this guard pins the contract regardless of pipeline config.
        """
        result = self._pipeline.search(query=query)
        out: list[KairixNativeMemory] = []
        for hit in result.results[:top_k]:
            out.append(self._hit_to_memory(hit))
        return out

    def update(self, memory_id: str, content: str) -> None:
        """Rewrite the content portion of an existing memory file."""
        path = self._memory_path(memory_id)
        if not path.exists():
            raise KeyError(f"KairixNativeMemoryStore: no memory with id {memory_id!r}")
        existing_md = self._parse_frontmatter(path.read_text(encoding="utf-8"))
        path.write_text(self._render_markdown(existing_md, content), encoding="utf-8")

    def delete(self, memory_id: str) -> None:
        """Remove the memory file; no-op if it does not exist."""
        path = self._memory_path(memory_id)
        path.unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _memory_path(self, mem_id: str) -> Path:
        return self._paths.document_root / _MEMORIES_SUBDIR / f"{mem_id}.md"

    @staticmethod
    def _content_hash_id(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()[:_ID_PREFIX_LEN]

    @staticmethod
    def _render_markdown(metadata: dict[str, Any], content: str) -> str:
        """Emit YAML-frontmatter + body in the shape kairix embed expects."""
        if not metadata:
            return content + "\n"
        fm_lines = "\n".join(f"{k}: {v}" for k, v in metadata.items())
        return f"---\n{fm_lines}\n---\n{content}\n"

    @staticmethod
    def _parse_frontmatter(text: str) -> dict[str, Any]:
        """Extract the YAML-frontmatter dict, tolerant of missing/malformed.

        Deliberately minimal — only handles ``key: value`` lines. Anything
        more elaborate (nested keys, lists) is a write-only metadata feature
        that ``add`` does not produce, so the round-trip stays simple.
        """
        if not text.startswith("---\n"):
            return {}
        end = text.find("\n---\n", 4)
        if end == -1:
            return {}
        md: dict[str, Any] = {}
        for line in text[4:end].splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                md[k.strip()] = v.strip()
        return md

    @staticmethod
    def _hit_to_memory(hit: Any) -> KairixNativeMemory:
        """Map a SearchPipeline ``BudgetedResult`` row to a Memory.

        ``SearchPipeline.search`` returns
        :class:`kairix.core.search.budget.BudgetedResult` rows: the
        document identity (path/title/collection/score) lives on the
        wrapped ``FusedResult`` at ``hit.result``, and the tier-selected
        text at ``hit.content`` (same projection as
        :func:`kairix.use_cases.search.run_search`).

        The id we surface is the file stem — the same id :meth:`add`
        returns — so it round-trips into :meth:`update` / :meth:`delete`
        (which resolve ``document_root/memories/<id>.md``). The
        document-relative path is kept in ``metadata["path"]``.
        """
        inner = getattr(hit, "result", None)
        path = str(getattr(inner, "path", "") or "")
        content = getattr(hit, "content", "") or getattr(inner, "snippet", "") or ""
        return KairixNativeMemory(
            id=PurePosixPath(path).stem if path else "",
            content=str(content),
            score=float(getattr(inner, "boosted_score", 0.0) or 0.0),
            metadata={
                "path": path,
                "title": str(getattr(inner, "title", "") or ""),
                "collection": str(getattr(inner, "collection", "") or ""),
                "tier": str(getattr(hit, "tier", "") or ""),
                "tokens": int(getattr(hit, "token_estimate", 0) or 0),
            },
        )
