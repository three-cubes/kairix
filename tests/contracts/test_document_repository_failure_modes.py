"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`DocumentRepository`.

Five Protocol methods: ``search_fts`` / ``get_by_path`` /
``list_chunk_seqs`` / ``get_chunk_dates`` / ``insert_or_update``.

Every body runs over BOTH the production
:class:`kairix.core.db.repository.SQLiteDocumentRepository` (a real
SQLite file under ``tmp_path``; backend failure injected through its
public ``opener`` DI seam) and the canonical
:class:`tests.fakes.FakeDocumentRepository` (F43 behavioural parity).

Parity finding (PLA-472): the production repository is NEVER-RAISES on
backend failure — ``search_fts`` returns ``[]``, ``get_by_path`` ``None``,
``get_chunk_dates`` ``{}``, ``list_chunk_seqs`` ``[]``, and
``insert_or_update`` logs a WARNING and DROPS the write. The previous
contract asserted the opposite (``search_fts`` / ``insert_or_update``
raise) against fake-only bodies, so the silent-write-drop in production
was never pinned. The fake gained a faithful ``backend_unavailable=True``
mode and these bodies now pin the real observable on both impls.

Each test carries a "Sabotage proof:" comment describing the mutation
that proves the assertion has teeth.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from kairix.core.db.repository import SQLiteDocumentRepository
from kairix.core.db.schema import create_schema
from kairix.core.protocols import DocumentRepository
from tests.fakes import FakeDocumentRepository

pytestmark = pytest.mark.contract

# A factory takes ``(tmp_path, backend_available)``.
RepoFactory = Callable[[Path, bool], DocumentRepository]


def _locked_opener(_path: object) -> sqlite3.Connection:
    raise sqlite3.OperationalError("F68-database-is-locked")


def _real_repo(tmp_path: Path, backend_available: bool) -> DocumentRepository:
    db_path = tmp_path / "index.sqlite"
    if not backend_available:
        return SQLiteDocumentRepository(db_path, opener=_locked_opener)
    db = sqlite3.connect(str(db_path))
    create_schema(db)
    db.commit()
    db.close()
    return SQLiteDocumentRepository(db_path)


def _fake_repo(_tmp_path: Path, backend_available: bool) -> DocumentRepository:
    return FakeDocumentRepository(documents=[], backend_unavailable=not backend_available)


_IMPLEMENTATIONS: list[tuple[str, RepoFactory]] = [
    ("real", _real_repo),
    ("fake", _fake_repo),
]


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_search_fts_returns_empty_when_backend_unavailable(name: str, factory: RepoFactory, tmp_path: Path) -> None:
    """When the backend cannot be opened (SQLite locked, FTS5 corrupt)
    ``search_fts`` returns ``[]`` — the documented never-raises shape the
    hybrid pipeline relies on to degrade to vector-only.

    Sabotage proof (executed): in ``SQLiteDocumentRepository.search_fts``
    replace the open-failure ``return []`` with ``raise``. Re-run: the
    ``real`` case fails with the injected OperationalError. Restored.
    """
    repo = factory(tmp_path, False)
    assert repo.search_fts("anything") == [], name


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_get_by_path_returns_empty_none_when_path_absent(name: str, factory: RepoFactory, tmp_path: Path) -> None:
    """``get_by_path`` for an unknown path returns ``None`` — the
    documented "no document" sentinel. Callers distinguish from raised
    exception.

    Sabotage proof: in ``SQLiteDocumentRepository.get_by_path`` change the
    ``if row is None: return None`` to ``return {"path": path}``. Re-run:
    the ``real`` case fails. Restored.
    """
    repo = factory(tmp_path, True)
    repo.insert_or_update("a.md", "default", "alpha", "body", "hash-a")
    assert repo.get_by_path("missing.md") is None, name
    # Positive control: the written path IS readable on both impls.
    found = repo.get_by_path("a.md")
    assert found is not None and found["title"] == "alpha", name


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_get_chunk_dates_returns_empty_when_no_paths_match(name: str, factory: RepoFactory, tmp_path: Path) -> None:
    """``get_chunk_dates`` for unknown paths returns an empty mapping
    — callers use this to skip date-boost when no chunk dates exist.

    Sabotage proof: in ``SQLiteDocumentRepository._get_chunk_dates_uncached``
    change the final ``return result`` to ``return {"ghost.md": "2026-01-01"}``.
    Re-run: the ``real`` case fails. Restored.
    """
    repo = factory(tmp_path, True)
    assert repo.get_chunk_dates(["missing-a.md", "missing-b.md"]) == {}, name


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_insert_or_update_returns_empty_when_backend_rejects(name: str, factory: RepoFactory, tmp_path: Path) -> None:
    """When the backend rejects the write, ``insert_or_update`` does NOT
    raise — production logs a WARNING and the write is dropped, so a
    subsequent read finds nothing (pins today's silent-drop behaviour;
    see the PLA-472 finding in the module docstring).

    Sabotage proof (executed): in ``SQLiteDocumentRepository.insert_or_update``
    re-raise from the ``except (sqlite3.Error, OSError)`` branch. Re-run:
    the ``real`` case fails with the injected OperationalError. Restored.
    """
    repo = factory(tmp_path, False)
    assert repo.insert_or_update("a.md", "default", "t", "c", "h") is None, name
    assert repo.get_by_path("a.md") is None, name


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_list_chunk_seqs_returns_empty_when_backend_open_fails(name: str, factory: RepoFactory, tmp_path: Path) -> None:
    """``list_chunk_seqs`` MUST return ``[]`` (never raise) when the SQLite
    backend can't be opened — a source_uri-only expand degrades to the
    doc-level fallback instead of crashing the L2 handoff (PLA-297).

    Failure is injected through the public ``opener`` DI seam (no
    monkeypatch, F1-clean); the observable outcome is the empty list.

    Sabotage proof: EXECUTED — dropping the ``except (sqlite3.Error,
    OSError)`` guard in ``SQLiteDocumentRepository.list_chunk_seqs`` lets the
    injected ``OperationalError`` propagate and the ``real`` case fails.
    Restored.
    """
    repo = factory(tmp_path, False)
    assert repo.list_chunk_seqs("m365://doc-alpha") == [], name
