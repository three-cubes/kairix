"""Additional unit tests for entities CLI lifting coverage above 90%.

The existing ``test_cli.py`` covers formatters and individual cmd_*
functions in isolation. This module fills the remaining gaps:

  - The ``cmd_seed`` happy path with candidates + Neo4j seeding.
  - The ``cmd_seed`` default db_path resolution branch.
  - The ``main()`` dispatcher's suggest / validate / get branches.
"""

from __future__ import annotations

import io
import sqlite3
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

import pytest

import kairix.knowledge.entities.cli as entities_cli

pytestmark = pytest.mark.unit


def _capture(fn: Any) -> tuple[int, str, str]:
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    rc = 0
    try:
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            rc = int(fn())
    except SystemExit as e:
        rc = int(e.code) if e.code is not None else 0
    return rc, out_buf.getvalue(), err_buf.getvalue()


# ---------------------------------------------------------------------------
# cmd_seed — happy path + branches not covered by test_seed_cli.py
# ---------------------------------------------------------------------------


class _AvailableNeo:
    available = True


class _UnavailableNeo:
    available = False


def _populated_db(path: Path) -> None:
    db = sqlite3.connect(str(path))
    db.execute("CREATE TABLE documents (id INTEGER, path TEXT, title TEXT, active INTEGER)")
    db.execute("INSERT INTO documents VALUES (1, 'a.md', 'A', 1)")
    db.commit()
    db.close()


def test_seed_with_candidates_lists_and_seeds_neo4j(monkeypatch, tmp_path: Path) -> None:
    """seed with non-empty candidate list + available Neo4j → seed_graph called."""
    import kairix.knowledge.entities.seed as seed_mod

    db_path = tmp_path / "index.sqlite"
    _populated_db(db_path)

    from types import SimpleNamespace

    fake_candidates = [
        SimpleNamespace(
            entity_type="ORG",
            name=f"Entity{i:02d}",
            confidence=0.9,
            source_docs=["a.md"],
        )
        for i in range(25)
    ]
    monkeypatch.setattr(seed_mod, "scan_for_entities", lambda db, limit: fake_candidates)
    seed_calls: list[int] = []

    def _fake_seed_graph(neo: Any, candidates: list) -> int:
        seed_calls.append(len(candidates))
        return len(candidates)

    monkeypatch.setattr(seed_mod, "seed_graph", _fake_seed_graph)

    rc = entities_cli.main(["seed"], db_path=db_path, neo4j_client=_AvailableNeo())
    assert rc == 0
    assert seed_calls == [25]


def test_seed_with_candidates_dry_run_does_not_call_seed_graph(monkeypatch, tmp_path: Path) -> None:
    import kairix.knowledge.entities.seed as seed_mod

    db_path = tmp_path / "index.sqlite"
    _populated_db(db_path)

    from types import SimpleNamespace

    monkeypatch.setattr(
        seed_mod,
        "scan_for_entities",
        lambda db, limit: [SimpleNamespace(entity_type="ORG", name="E1", confidence=0.9, source_docs=["a.md"])],
    )
    called = []
    monkeypatch.setattr(seed_mod, "seed_graph", lambda neo, candidates: called.append("yes"))

    rc = entities_cli.main(["seed", "--dry-run"], db_path=db_path, neo4j_client=_AvailableNeo())
    assert rc == 0
    assert called == [], "seed_graph must not be called in --dry-run mode"


def test_seed_exits_1_when_neo4j_unavailable(monkeypatch, tmp_path: Path) -> None:
    import kairix.knowledge.entities.seed as seed_mod

    db_path = tmp_path / "index.sqlite"
    _populated_db(db_path)

    from types import SimpleNamespace

    monkeypatch.setattr(
        seed_mod,
        "scan_for_entities",
        lambda db, limit: [SimpleNamespace(entity_type="ORG", name="E1", confidence=0.9, source_docs=["a.md"])],
    )

    rc = entities_cli.main(["seed"], db_path=db_path, neo4j_client=_UnavailableNeo())
    assert rc == 1


def test_seed_default_db_path_resolves_from_core_db(monkeypatch, tmp_path: Path) -> None:
    """When db_path is None, the CLI calls get_db_path() to resolve it."""
    import kairix.core.db as core_db
    import kairix.knowledge.entities.seed as seed_mod

    real_db = tmp_path / "k.db"
    _populated_db(real_db)
    monkeypatch.setattr(core_db, "get_db_path", lambda: str(real_db))
    monkeypatch.setattr(seed_mod, "scan_for_entities", lambda db, limit: [])

    # neo4j_client unused because no candidates.
    rc = entities_cli.main(["seed"])
    assert rc == 0


def test_seed_default_neo4j_client_resolves_from_graph_client(monkeypatch, tmp_path: Path) -> None:
    """When neo4j_client is None, the CLI calls get_client() (graph_client.get_client)."""
    from types import SimpleNamespace

    import kairix.knowledge.entities.seed as seed_mod
    import kairix.knowledge.graph.client as graph_client

    db_path = tmp_path / "index.sqlite"
    _populated_db(db_path)

    monkeypatch.setattr(
        seed_mod,
        "scan_for_entities",
        lambda db, limit: [SimpleNamespace(entity_type="ORG", name="E1", confidence=0.9, source_docs=["a.md"])],
    )
    seed_calls: list[int] = []
    monkeypatch.setattr(seed_mod, "seed_graph", lambda neo, cands: seed_calls.append(len(cands)) or 1)
    monkeypatch.setattr(graph_client, "get_client", lambda: _AvailableNeo())

    rc = entities_cli.main(["seed"], db_path=db_path)
    assert rc == 0
    assert seed_calls == [1]


# ---------------------------------------------------------------------------
# main() — dispatcher branches not covered by direct cmd_* tests.
# ---------------------------------------------------------------------------


def test_main_dispatches_suggest(monkeypatch) -> None:
    """main() routes 'suggest' to cmd_suggest."""
    from types import SimpleNamespace

    import kairix.use_cases.entity as entity_uc

    fake_out = SimpleNamespace(text="acme", suggestions=[], new_count=0, existing_count=0, error="")
    monkeypatch.setattr(entity_uc, "run_entity_suggest", lambda text, deps=None: fake_out)
    rc, stdout, _ = _capture(lambda: entities_cli.main(["suggest", "acme is a client"]))
    assert rc == 0
    assert "Total: 0 entities found" in stdout


def test_main_dispatches_validate(monkeypatch) -> None:
    """main() routes 'validate' to cmd_validate."""
    from types import SimpleNamespace

    import kairix.use_cases.entity as entity_uc

    fake_out = SimpleNamespace(
        name="Acme",
        neo4j_id="acme",
        matches=[],
        updated=False,
        error="",
    )
    monkeypatch.setattr(entity_uc, "run_entity_validate", lambda name, update=False, deps=None: fake_out)
    rc, _stdout, _ = _capture(lambda: entities_cli.main(["validate", "Acme"]))
    # No matches → return 1
    assert rc == 1


def test_main_dispatches_get(monkeypatch) -> None:
    """main() routes 'get' to cmd_get."""
    from types import SimpleNamespace

    import kairix.use_cases.entity_get as entity_get_uc

    fake_out = SimpleNamespace(
        id="acme",
        name="Acme",
        type="Organisation",
        summary="supplier",
        vault_path="/Acme.md",
        error="",
    )
    monkeypatch.setattr(entity_get_uc, "run_entity_get", lambda name, deps=None: fake_out)
    rc, stdout, _ = _capture(lambda: entities_cli.main(["get", "Acme"]))
    assert rc == 0
    assert "Acme" in stdout
