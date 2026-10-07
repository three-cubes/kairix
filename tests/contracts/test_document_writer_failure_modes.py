"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`DocumentWriter`.

Single Protocol method ``write(*, corpus_id, session_id, rendered_body,
frontmatter)`` returning a :class:`Path`. When the underlying writer
rejects the write (disk full, FTS5 rebuild error, permission denied),
the exception MUST propagate so the caller's
:class:`IngestResult.document_paths` doesn't lie about what landed on
disk.

There is NO production :class:`DocumentWriter` in ``kairix/`` yet —
``kairix.corpus.wiring.make_production_document_writer`` is a Phase 2
deferral that raises ``NotImplementedError``. Per the PLA-472 ruling the
Protocol contract is therefore proved over a minimal Protocol-compliant
probe (``_MarkdownProbeWriter`` — the shape the Phase 2
``MarkdownDocumentWriter`` will take: one markdown file per session under
a root directory) AND the canonical :class:`tests.fakes.FakeDocumentWriter`
(F43 behavioural parity). When the production writer lands, add it to
``_IMPLEMENTATIONS``.

Each test carries a "Sabotage proof:" comment describing the mutation
that proves the assertion has teeth.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from kairix.core.protocols import DocumentWriter
from tests.fakes import FakeDocumentWriter

pytestmark = pytest.mark.contract


class _MarkdownProbeWriter:
    """Minimal Protocol-compliant writer: ``<root>/<corpus>/<session>.md``."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def write(self, *, corpus_id: str, session_id: str, rendered_body: str, frontmatter: dict[str, Any]) -> Path:
        target = self._root / corpus_id / f"{session_id}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        fm = "".join(f"{k}: {v}\n" for k, v in frontmatter.items())
        target.write_text(f"---\n{fm}---\n{rendered_body}\n", encoding="utf-8")
        return target


# A factory takes ``(tmp_path, store_writable)``; ``False`` builds a
# writer whose backing store rejects every write.
WriterFactory = Callable[[Path, bool], DocumentWriter]


def _probe_writer(tmp_path: Path, store_writable: bool) -> DocumentWriter:
    root = tmp_path / "documents"
    if not store_writable:
        root.parent.mkdir(parents=True, exist_ok=True)
        root.write_text("not a directory", encoding="utf-8")  # every write is rejected
    return _MarkdownProbeWriter(root)


def _fake_writer(tmp_path: Path, store_writable: bool) -> DocumentWriter:
    if store_writable:
        return FakeDocumentWriter(base_path=tmp_path / "documents")
    return FakeDocumentWriter(raises=NotADirectoryError("documents root unavailable"))


_IMPLEMENTATIONS: list[tuple[str, WriterFactory]] = [
    ("probe", _probe_writer),
    ("fake", _fake_writer),
]

_WRITE_KWARGS: dict[str, Any] = {
    "corpus_id": "corpus-alpha",
    "session_id": "session-001",
    "rendered_body": "body",
    "frontmatter": {"agent": "agent-alpha"},
}


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_write_raises_when_backend_rejects_persistence(name: str, factory: WriterFactory, tmp_path: Path) -> None:
    """A ``DocumentWriter`` whose store rejects the write must surface the
    exception — silent fallback to a sentinel path would let the caller
    record a doc that never landed. A healthy store returns the path the
    document landed at.

    Sabotage proof (executed): in ``FakeDocumentWriter.write`` change
    ``raise self._raises`` to ``return Path("/fake/sentinel.md")``. Re-run:
    the ``fake`` case fails because no exception fires. Restored.
    """
    with pytest.raises(OSError):
        factory(tmp_path / "rejected", False).write(**_WRITE_KWARGS)
    landed = factory(tmp_path / "healthy", True).write(**_WRITE_KWARGS)
    assert landed == tmp_path / "healthy" / "documents" / "corpus-alpha" / "session-001.md", name
