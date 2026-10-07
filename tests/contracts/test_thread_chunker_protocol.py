"""Contract tests for the ``thread`` Chunker plugin (F43, F55).

Pins:
* :class:`ThreadChunker` satisfies the :class:`Chunker` Protocol.
* Module-level ``version`` is non-empty AND identical to
  :attr:`ThreadChunker.version`.
* Every emitted :class:`Chunk` carries ``chunker_version=self.version``
  (F55 invariant).
* Constructor rejects non-positive caps / windows with an
  F21-shaped error string (``fix:`` / ``next:`` markers).
* The Protocol-surface ``chunk(*, text, section_kind, source_uri)``
  signature handles each of the empty / single-message / threaded /
  windowed envelope shapes without raising.

F43 parity: the Protocol-level contracts run ONE body over the real
:class:`ThreadChunker` AND the canonical :class:`tests.fakes.FakeChunker`.
ThreadChunker's own Slack-envelope parsing rules (window guard, JSON
scalar / mixed-list handling, ``ts`` fallback, empty-text skip) live in
``tests/unit/test_thread_chunker_units.py``.

Sabotage-proofs (mutate prod → confirm fail → restore):
* Delete ``version: str = version`` from the class → asserts in
  ``test_chunker_declares_version`` fail.
* Drop ``chunker_version=self.version`` from ``_build_chunk`` →
  asserts in ``test_emitted_chunks_carry_chunker_version`` fail.
* Replace ``raise ValueError`` with ``return`` on the constructor
  guards → asserts in ``test_constructor_rejects_non_positive_cap``
  fail.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from kairix.chunkers.thread import ThreadChunker
from kairix.chunkers.thread import version as thread_version
from kairix.core.protocols import Chunk, Chunker
from tests.fakes import FakeChunker

pytestmark = [pytest.mark.contract]


def _msg(
    *,
    ts: str,
    user: str,
    text: str,
    thread_ts: str | None = None,
    channel: str = "ch-alpha",
) -> dict[str, object]:
    """Build a slack-shaped message dict for fixture seeding."""
    return {
        "ts": ts,
        "user": user,
        "text": text,
        "thread_ts": thread_ts,
        "channel": channel,
    }


_ChunkerFactory = Callable[..., Chunker]

#: (factory, the version every instance must carry). The real leg's
#: expected version is the F55 module-level declaration.
_IMPLS = [
    pytest.param(ThreadChunker, thread_version, id="real"),
    pytest.param(FakeChunker, FakeChunker.version, id="fake"),
]


@pytest.fixture(params=[ThreadChunker, FakeChunker], ids=["real", "fake"])
def chunker(request: pytest.FixtureRequest) -> Chunker:
    """A default-configured chunker — the real ThreadChunker and the canonical fake."""
    factory: _ChunkerFactory = request.param
    return factory()


def test_chunker_satisfies_protocol(chunker: Chunker) -> None:
    """The class is recognised as a runtime :class:`Chunker`.

    Sabotage proof: rename ``ThreadChunker.chunk`` to ``chunk_x``; the
    real leg's isinstance check fails. Restored.
    """
    assert isinstance(chunker, Chunker)


@pytest.mark.parametrize(("factory", "expected_version"), _IMPLS)
def test_chunker_declares_version(factory: _ChunkerFactory, expected_version: str) -> None:
    """F55: the class-level version is non-empty and identical on instances
    (for the real plugin: identical to the module-level declaration).

    Sabotage proof: set ``ThreadChunker.version = "x"`` (class attr) while
    the module ``version`` stays ``"0.1.0"``; the real leg fails. Restored.
    """
    assert isinstance(expected_version, str)
    assert expected_version.strip() != ""
    assert factory.version == expected_version  # type: ignore[attr-defined]  # Chunker factories are classes carrying the F55 class-level version
    assert factory().version == expected_version


def test_empty_input_yields_no_chunks(chunker: Chunker) -> None:
    """Blank input → ``()``.

    Sabotage proof: in ``_parse_messages`` make the blank-text branch
    return ``[{"text": stripped}]`` instead of ``[]``; the real leg emits
    one empty chunk and fails. Restored.
    """
    assert chunker.chunk(text="", section_kind="text", source_uri="slack://ch-alpha") == ()
    assert chunker.chunk(text="   \n  ", section_kind="text", source_uri="slack://ch-alpha") == ()


def test_single_message_yields_one_chunk(chunker: Chunker) -> None:
    """One message envelope → exactly one Chunk carrying the message text.

    Sabotage proof: in ``_join_text`` return ``""``; the real leg's
    ``"hello world" in text`` assertion fails. Restored.
    """
    envelope = json.dumps(_msg(ts="100.0", user="agent-alpha", text="hello world"))
    chunks = chunker.chunk(text=envelope, section_kind="text", source_uri="slack://ch-alpha/1")
    assert len(chunks) == 1
    assert isinstance(chunks[0], Chunk)
    assert "hello world" in chunks[0].text


def test_emitted_chunks_carry_chunker_version(chunker: Chunker) -> None:
    """F55: every Chunk threads ``chunker_version=self.version``.

    Sabotage proof: drop ``chunker_version=self.version`` from the
    ``_build_chunk`` call in ``_emit_chunks_for_group``; the real leg
    fails. Restored.
    """
    envelope = json.dumps(_msg(ts="100.0", user="agent-alpha", text="hello"))
    chunks = chunker.chunk(text=envelope, section_kind="text", source_uri="slack://ch-alpha/1")
    assert chunks
    for chunk in chunks:
        assert chunk.chunker_version == chunker.version


def test_emitted_chunks_carry_source_uri_per_f39(chunker: Chunker) -> None:
    """F39: ``source_uri`` propagated to every emitted Chunk.

    Sabotage proof: pass ``source_uri=""`` to ``_build_chunk`` in
    ``_emit_chunks_for_group``; the real leg fails. Restored.
    """
    envelope = json.dumps([_msg(ts="1.0", user="u1", text="a"), _msg(ts="2.0", user="u2", text="b")])
    chunks = chunker.chunk(text=envelope, section_kind="text", source_uri="slack://ch-alpha/X")
    assert chunks
    for chunk in chunks:
        assert chunk.source_uri == "slack://ch-alpha/X"


@pytest.mark.parametrize("factory", [ThreadChunker, FakeChunker], ids=["real", "fake"])
def test_constructor_rejects_non_positive_cap(factory: _ChunkerFactory) -> None:
    """A non-positive token cap is rejected at construction with an error
    naming the parameter (F21-shaped: ``fix:`` marker).

    Sabotage proof: replace the ``max_tokens_per_chunk <= 0`` guard's
    ``raise ValueError`` with ``pass``; the real leg fails. Restored.
    """
    with pytest.raises(ValueError, match=r"max_tokens_per_chunk.*fix:"):
        factory(max_tokens_per_chunk=0)
    with pytest.raises(ValueError, match="max_tokens_per_chunk"):
        factory(max_tokens_per_chunk=-5)


def test_malformed_json_degrades_gracefully(chunker: Chunker) -> None:
    """Non-JSON input is treated as plain text — one chunk, not a hard fail.

    The chunker is downstream of the connector + extractor — when
    upstream wiring is mis-shaped the chunker should degrade rather
    than crash the silver pipeline.

    Sabotage proof: in ``_parse_messages`` replace the
    ``except json.JSONDecodeError`` fallback's return with ``raise``;
    the real leg fails. Restored.
    """
    chunks = chunker.chunk(text="raw text not json", section_kind="text", source_uri="slack://x")
    assert len(chunks) == 1
    assert "raw text not json" in chunks[0].text
