"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`CorpusEmbedder`.

Single Protocol method ``embed(paths_to_embed)``. The docstring pins
the failure surface: an empty tuple is a legal no-op signal (returns
``0`` chunks indexed). A raise from the underlying embed pipeline
must propagate.

There is NO production :class:`CorpusEmbedder` in ``kairix/`` yet —
``kairix.corpus.wiring.make_production_embedder`` is a Phase 2 deferral
that raises ``NotImplementedError``. Per the PLA-472 ruling the Protocol
contract is therefore proved over a minimal Protocol-compliant probe
(``_PipelineProbeEmbedder`` — the shape the Phase 2
``EmbedPipelineEmbedder`` will take: delegate the paths to an embed
pipeline callable and return its chunk count) AND the canonical
:class:`tests.fakes.FakeCorpusEmbedder` (F43 behavioural parity). When
the production embedder lands, add it to ``_IMPLEMENTATIONS``.

Each test carries a "Sabotage proof:" comment describing the mutation
that proves the assertion has teeth.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from kairix.core.protocols import CorpusEmbedder
from tests.fakes import FakeCorpusEmbedder

pytestmark = pytest.mark.contract


class _PipelineProbeEmbedder:
    """Minimal Protocol-compliant embedder delegating to a pipeline callable."""

    def __init__(self, pipeline: Callable[[tuple[Path, ...]], int]) -> None:
        self._pipeline = pipeline

    def embed(self, paths_to_embed: tuple[Path, ...]) -> int:
        if not paths_to_embed:
            return 0
        return self._pipeline(tuple(paths_to_embed))


# A factory takes the error the embed pipeline raises (``None`` = healthy).
EmbedderFactory = Callable[[BaseException | None], CorpusEmbedder]


def _probe_embedder(error: BaseException | None) -> CorpusEmbedder:
    def _pipeline(paths: tuple[Path, ...]) -> int:
        if error is not None:
            raise error
        return len(paths)

    return _PipelineProbeEmbedder(_pipeline)


def _fake_embedder(error: BaseException | None) -> CorpusEmbedder:
    return FakeCorpusEmbedder(raises=error)


_IMPLEMENTATIONS: list[tuple[str, EmbedderFactory]] = [
    ("probe", _probe_embedder),
    ("fake", _fake_embedder),
]


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_embed_returns_empty_count_when_paths_empty(name: str, factory: EmbedderFactory) -> None:
    """``embed(())`` is the documented no-op signal; the embedder MUST
    return ``0`` chunks indexed (not raise on the empty tuple, not
    invent ghost chunks).

    Sabotage proof: in :meth:`FakeCorpusEmbedder.embed` change
    ``return 0`` (in the empty-scripted branch) to ``return 99``.
    Re-run: the ``fake`` case fails because the call returns 99. Restored.
    """
    embedder = factory(None)
    chunks_indexed = embedder.embed(paths_to_embed=())
    assert chunks_indexed == 0, f"{name}: empty paths must return 0; got {chunks_indexed}"


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_embed_raises_when_underlying_pipeline_crashes(name: str, factory: EmbedderFactory) -> None:
    """An embedder whose pipeline raises must surface the exception —
    silent fallback to ``0`` would mask broken embedding state and let
    downstream ``IngestResult.chunks_indexed`` lie about coverage.

    Sabotage proof (executed): in ``FakeCorpusEmbedder.embed`` change
    ``raise self._raises`` to ``return 0``. Re-run: the ``fake`` case
    fails because no exception fires. Restored.
    """
    embedder = factory(RuntimeError("F68-embedder-cuda-oom"))
    with pytest.raises(RuntimeError, match="F68-embedder-cuda-oom"):
        embedder.embed(paths_to_embed=(Path("/fake/doc.md"),))
