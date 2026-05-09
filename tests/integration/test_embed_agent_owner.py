"""Integration test for embed-side agent_owner tagging (#114).

Indexes the synthetic-agent fixture under
``04-Agent-Knowledge/<agent>/memory/...`` and asserts that the embed
pipeline (scanner + agent_owner_resolver wired through ConfigDrivenAgentRegistry)
tags each document with its owning agent. Documents outside any agent's
write_path land with ``agent_owner=NULL``.

Issue spec: each chunk under a path matching an agent's ``write_path``
should carry that agent's name; cross-agent / shared / unowned documents
remain NULL.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from kairix.core.db.scanner import CollectionConfig, DocumentScanner
from kairix.core.db.schema import create_schema
from kairix.core.search.registry import (
    AgentDef,
    ConfigDrivenAgentRegistry,
    build_agent_owner_resolver,
)

pytestmark = pytest.mark.integration

_SYNTHETIC_AGENTS_DIR = Path(__file__).parent.parent / "fixtures" / "synthetic_agents"


def _build_indexed_db(tmp_root: Path) -> sqlite3.Connection:
    """Copy the synthetic-agent fixture into ``tmp_root`` and run the scanner
    with an agent_owner resolver derived from a two-agent registry.
    """
    shutil.copytree(_SYNTHETIC_AGENTS_DIR / "04-Agent-Knowledge", tmp_root / "04-Agent-Knowledge")

    db = sqlite3.connect(":memory:")
    create_schema(db)

    registry = ConfigDrivenAgentRegistry(
        agents=[
            AgentDef(
                name="agent-alpha",
                paths=["04-Agent-Knowledge/agent-alpha"],
                write_path="04-Agent-Knowledge/agent-alpha",
            ),
            AgentDef(
                name="agent-beta",
                paths=["04-Agent-Knowledge/agent-beta"],
                write_path="04-Agent-Knowledge/agent-beta",
            ),
        ]
    )
    resolver = build_agent_owner_resolver(registry)
    scanner = DocumentScanner(db, document_root=tmp_root, agent_owner_resolver=resolver)
    scanner.scan([CollectionConfig(name="agent-knowledge", path="04-Agent-Knowledge")])
    return db


@pytest.mark.integration
def test_embed_pipeline_tags_documents_with_owning_agent(tmp_path: Path) -> None:
    """Every document under ``04-Agent-Knowledge/agent-alpha/`` lands with
    ``agent_owner='agent-alpha'``; same for ``agent-beta``.
    """
    db = _build_indexed_db(tmp_path)

    alpha_owners = db.execute(
        "SELECT agent_owner FROM documents WHERE path LIKE ? AND active = 1",
        ("04-Agent-Knowledge/agent-alpha/%",),
    ).fetchall()
    assert alpha_owners, "fixture should index at least one agent-alpha document"
    assert all(row[0] == "agent-alpha" for row in alpha_owners), (
        f"alpha documents not all tagged with agent-alpha: {alpha_owners}"
    )

    beta_owners = db.execute(
        "SELECT agent_owner FROM documents WHERE path LIKE ? AND active = 1",
        ("04-Agent-Knowledge/agent-beta/%",),
    ).fetchall()
    assert beta_owners, "fixture should index at least one agent-beta document"
    assert all(row[0] == "agent-beta" for row in beta_owners), (
        f"beta documents not all tagged with agent-beta: {beta_owners}"
    )


@pytest.mark.integration
def test_embed_pipeline_leaves_unowned_documents_with_null_agent_owner(tmp_path: Path) -> None:
    """Documents under ``04-Agent-Knowledge/entities/`` are not under any
    agent's ``write_path`` and must be persisted with ``agent_owner=NULL``
    (the canonical 'shared / unowned' marker).
    """
    db = _build_indexed_db(tmp_path)

    entity_rows = db.execute(
        "SELECT path, agent_owner FROM documents WHERE path LIKE ? AND active = 1",
        ("04-Agent-Knowledge/entities/%",),
    ).fetchall()
    assert entity_rows, "fixture should index at least one entity document"
    for path, owner in entity_rows:
        assert owner is None, f"entity document {path!r} should have NULL agent_owner, got {owner!r}"


@pytest.mark.integration
def test_embed_pipeline_filters_per_agent_via_agent_owner_column(tmp_path: Path) -> None:
    """The downstream selection contract: ``WHERE agent_owner = ?`` returns
    exactly that agent's documents, never another agent's, never shared.

    This is the test that proves the column is *useful* — not just present.
    """
    db = _build_indexed_db(tmp_path)

    alpha_paths = {
        row[0]
        for row in db.execute(
            "SELECT path FROM documents WHERE active = 1 AND agent_owner = ?", ("agent-alpha",)
        ).fetchall()
    }
    beta_paths = {
        row[0]
        for row in db.execute(
            "SELECT path FROM documents WHERE active = 1 AND agent_owner = ?", ("agent-beta",)
        ).fetchall()
    }

    assert alpha_paths, "agent-alpha should own at least one document"
    assert beta_paths, "agent-beta should own at least one document"
    assert alpha_paths.isdisjoint(beta_paths), f"alpha and beta path sets overlap: {alpha_paths & beta_paths}"
    assert all(p.startswith("04-Agent-Knowledge/agent-alpha/") for p in alpha_paths), (
        f"agent-alpha column leaked non-alpha paths: {alpha_paths}"
    )
    assert all(p.startswith("04-Agent-Knowledge/agent-beta/") for p in beta_paths), (
        f"agent-beta column leaked non-beta paths: {beta_paths}"
    )
