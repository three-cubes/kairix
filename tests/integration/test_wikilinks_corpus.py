"""End-to-end integration tests for wikilink injection across a corpus.

Wires the full wikilinks pipeline across multiple files:
  - ``inject_file`` (reads/writes one file)
  - ``inject_wikilinks`` (parses segments, applies first-mention rule)
  - ``_find_already_linked`` + ``_entities_for_own_page`` cooperation

A ``tmp_path`` corpus of 3 markdown files plus a small ``WikiEntity``
set drives the test. ``FakePaths`` (from ``tests/fakes.py``) replaces
``KairixPaths.resolve()`` — no env-var monkeypatching (F4-clean).

What's covered here that unit + BDD don't catch:
  - The first-mention invariant across a multi-file corpus: each entity
    gets wrapped once per file, and subsequent mentions in the same
    file stay plain text.
  - Files with no entity matches are not modified at all (no spurious
    re-write side effects).
  - The own-page guard cooperates with the corpus walk: an entity's
    own file does not get a self-link.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kairix.knowledge.wikilinks.injector import inject_file, inject_wikilinks
from kairix.knowledge.wikilinks.resolver import WikiEntity
from tests.fakes import FakePaths

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entities() -> list[WikiEntity]:
    """A small entity set with name/alias triggers."""
    return [
        WikiEntity(
            name="Acme-Corp",
            aliases=["Acme Corp"],
            vault_path="04-Agent-Knowledge/shared/entities/acme-corp.md",
            link="[[Acme-Corp]]",
            entity_type="organisation",
        ),
        WikiEntity(
            name="OpenClaw",
            aliases=[],
            vault_path="04-Agent-Knowledge/shared/entities/openclaw.md",
            link="[[OpenClaw]]",
            entity_type="organisation",
        ),
        WikiEntity(
            name="Alice-Smith",
            aliases=["Alice Smith"],
            vault_path="04-Agent-Knowledge/shared/entities/alice-smith.md",
            link="[[Alice-Smith]]",
            entity_type="person",
        ),
    ]


def _write_corpus(doc_root: Path) -> dict[str, Path]:
    """Write 3 eligible markdown files under 04-Agent-Knowledge/.

    The injector's ``should_inject`` only accepts vault paths under the
    canonical eligible prefixes (``04-Agent-Knowledge/`` etc.), so
    placing files there is what makes them eligible for the file-write
    branch.
    """
    target = doc_root / "04-Agent-Knowledge" / "shared"
    target.mkdir(parents=True)
    files: dict[str, Path] = {}

    files["with_two_mentions"] = target / "with_two_mentions.md"
    files["with_two_mentions"].write_text(
        "Acme-Corp launched a product. Later, Acme-Corp shipped v2.\n",
        encoding="utf-8",
    )

    files["with_multiple_entities"] = target / "with_multiple_entities.md"
    files["with_multiple_entities"].write_text(
        "OpenClaw and Alice Smith met. OpenClaw runs on the platform.\n",
        encoding="utf-8",
    )

    files["no_matches"] = target / "no_matches.md"
    files["no_matches"].write_text(
        "This document mentions nothing relevant — just generic prose.\n",
        encoding="utf-8",
    )

    return files


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_first_mention_only_wraps_first_occurrence_per_file(tmp_path: Path) -> None:
    """In a file with two mentions of the same entity, only the FIRST
    gets wrapped. The second remains plain text. This is the
    load-bearing wikilinks invariant (rule 1 in the injector docstring).

    Sabotage: if ``_inject_in_text`` started linking every occurrence
    (e.g. removed the ``already_linked`` set guard), the modified text
    would contain TWO ``[[Acme-Corp]]`` tokens and this assertion fails.
    """
    doc_root = tmp_path / "vault"
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    paths = FakePaths(document_root=doc_root, workspace_root=workspace_root)

    files = _write_corpus(doc_root)
    log_path = tmp_path / "wikilinks-log.jsonl"

    injected = inject_file(
        str(files["with_two_mentions"]),
        _entities(),
        dry_run=False,
        paths=paths,
        log_path=log_path,
    )

    assert injected == ["Acme-Corp"]
    written = files["with_two_mentions"].read_text(encoding="utf-8")
    # First mention wrapped, second NOT.
    assert written.count("[[Acme-Corp]]") == 1
    assert "Acme-Corp shipped v2" in written  # bare second occurrence preserved.


def test_no_match_file_stays_untouched(tmp_path: Path) -> None:
    """A file whose content has no entity triggers must not be
    rewritten. ``inject_file`` returns an empty list and the file
    bytes are bit-identical to before.

    Sabotage: if ``inject_file`` unconditionally rewrote the file (even
    when ``injected`` was empty), the post-write content might shift
    (e.g. trailing-newline normalisation), and the byte-equality
    assertion would fire.
    """
    doc_root = tmp_path / "vault"
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    paths = FakePaths(document_root=doc_root, workspace_root=workspace_root)

    files = _write_corpus(doc_root)
    pre = files["no_matches"].read_bytes()
    log_path = tmp_path / "wikilinks-log.jsonl"

    injected = inject_file(
        str(files["no_matches"]),
        _entities(),
        dry_run=False,
        paths=paths,
        log_path=log_path,
    )

    assert injected == []
    post = files["no_matches"].read_bytes()
    assert pre == post


def test_corpus_walk_lands_one_link_per_entity_per_file(tmp_path: Path) -> None:
    """Across a 3-file corpus, the first-mention-only invariant holds
    PER FILE: each entity gets at most one link in each eligible file.
    A multi-entity file links each one once; a no-match file is
    untouched; the cross-file behaviour is independent (one file's
    wrap doesn't suppress the entity in another file).

    Sabotage: if the ``linked_entities`` set were shared across files
    (e.g. as a module-level global), the second file would never get
    OpenClaw wrapped — the assertion ``"OpenClaw" in injected_two``
    would fail.
    """
    doc_root = tmp_path / "vault"
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    paths = FakePaths(document_root=doc_root, workspace_root=workspace_root)

    files = _write_corpus(doc_root)
    log_path = tmp_path / "wikilinks-log.jsonl"

    # File 1: two Acme-Corp mentions → one wrap.
    injected_one = inject_file(
        str(files["with_two_mentions"]),
        _entities(),
        dry_run=False,
        paths=paths,
        log_path=log_path,
    )
    # File 2: OpenClaw twice + Alice Smith once → one wrap per entity.
    injected_two = inject_file(
        str(files["with_multiple_entities"]),
        _entities(),
        dry_run=False,
        paths=paths,
        log_path=log_path,
    )
    # File 3: no triggers → no wraps.
    injected_three = inject_file(
        str(files["no_matches"]),
        _entities(),
        dry_run=False,
        paths=paths,
        log_path=log_path,
    )

    assert injected_one == ["Acme-Corp"]
    assert "OpenClaw" in injected_two
    assert "Alice-Smith" in injected_two
    assert injected_three == []

    # Per-file invariant: each linked entity appears exactly once as a wikilink.
    body_two = files["with_multiple_entities"].read_text(encoding="utf-8")
    assert body_two.count("[[OpenClaw]]") == 1
    assert body_two.count("[[Alice-Smith]]") == 1
    # Bare second mention of OpenClaw retained.
    assert "OpenClaw runs on the platform" in body_two


def test_entity_does_not_self_link_on_its_own_page(tmp_path: Path) -> None:
    """The ``_entities_for_own_page`` guard suppresses self-linking. A
    document that LIVES at an entity's vault_path must not get its own
    name wrapped — preserves the convention that an entity's home page
    doesn't link to itself.

    Sabotage: if ``_entities_for_own_page`` returned an empty set
    (i.e. the guard was deleted), the entity's name in its own file
    body would get wrapped and ``"[[Acme-Corp]]"`` would appear in the
    modified content. The assertion below catches that.
    """
    doc_root = tmp_path / "vault"
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    paths = FakePaths(document_root=doc_root, workspace_root=workspace_root)

    entity = WikiEntity(
        name="Acme-Corp",
        aliases=[],
        vault_path="04-Agent-Knowledge/shared/entities/acme-corp.md",
        link="[[Acme-Corp]]",
        entity_type="organisation",
    )
    # Write the entity's OWN page at its vault_path. The body mentions itself.
    target = doc_root / "04-Agent-Knowledge" / "shared" / "entities"
    target.mkdir(parents=True)
    own_page = target / "acme-corp.md"
    own_body = "Acme-Corp is a fictional company. Founded in 1920.\n"
    own_page.write_text(own_body, encoding="utf-8")

    modified, injected = inject_wikilinks(
        own_body,
        [entity],
        source_path=str(own_page),
        paths=paths,
    )

    assert injected == []
    assert "[[Acme-Corp]]" not in modified
    assert modified == own_body  # bytes preserved exactly.
