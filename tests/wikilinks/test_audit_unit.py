"""Unit tests for kairix.knowledge.wikilinks.audit helpers (PR #247 QG).

The S3776 cognitive-complexity refactor (commit 21ff2e68) extracted small
helpers from ``find_unlinked_mentions`` and ``find_broken_links``. Sonar
treats each extracted line as new code and measures coverage per line, so
the parent function's existing tests don't bring the helpers up to 90%.

These tests drive each helper directly via module-attribute access (no
``from ... import _name``; F5-clean) so every branch lands in
``new_coverage``. Fakes replace ``get_entities`` and any
``KairixPaths``-backed I/O; no ``@patch`` on kairix internals (F1) and
no ``KAIRIX_*`` env-var monkeypatching (F2).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import kairix.knowledge.wikilinks.audit as audit_mod
from kairix.knowledge.wikilinks.resolver import WikiEntity
from tests.fakes import FakePaths

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _entity(
    name: str = "Acme-Corp",
    *,
    aliases: list[str] | None = None,
    vault_path: str = "02-Areas/Clients/Acme-Corp/",
    link: str | None = None,
    entity_type: str = "organisation",
) -> WikiEntity:
    """Build a WikiEntity with sensible defaults — keeps tests focused on
    the helper under test rather than entity construction."""
    return WikiEntity(
        name=name,
        aliases=aliases if aliases is not None else [name],
        vault_path=vault_path,
        link=link or f"[[{name}]]",
        entity_type=entity_type,
    )


@pytest.fixture
def vault_paths(tmp_path: Path):
    """Concrete on-disk FakePaths so ``should_inject`` semantics apply."""
    vault = tmp_path / "vault"
    workspaces = tmp_path / "workspaces"
    vault.mkdir()
    workspaces.mkdir()
    return FakePaths(document_root=vault, workspace_root=workspaces)


# ---------------------------------------------------------------------------
# _build_target_to_path — broken-link helper
# ---------------------------------------------------------------------------


def test_build_target_to_path_extracts_targets_from_link_field() -> None:
    """A plain ``[[Acme-Corp]]`` link yields ``{"Acme-Corp": vault_path}``."""
    e = _entity(name="Acme-Corp", vault_path="02-Areas/Clients/Acme-Corp/")
    mapping = audit_mod._build_target_to_path([e])
    assert mapping == {"Acme-Corp": "02-Areas/Clients/Acme-Corp/"}


def test_build_target_to_path_handles_alias_pipe_form() -> None:
    """``[[Gamma-Systems|Gamma Systems]]`` keys off the target, not the alias."""
    e = _entity(name="Gamma Systems", link="[[Gamma-Systems|Gamma Systems]]", vault_path="02-Areas/Clients/Gamma/")
    mapping = audit_mod._build_target_to_path([e])
    assert mapping == {"Gamma-Systems": "02-Areas/Clients/Gamma/"}


def test_build_target_to_path_skips_malformed_link() -> None:
    """An entity with a non-wikilink ``link`` field is silently ignored."""
    bad = _entity(name="X", link="not-a-wikilink", vault_path="x/")
    good = _entity(name="Y", link="[[Y]]", vault_path="y/")
    mapping = audit_mod._build_target_to_path([bad, good])
    assert mapping == {"Y": "y/"}


# ---------------------------------------------------------------------------
# _broken_link_rows
# ---------------------------------------------------------------------------


def test_broken_link_rows_reports_missing_target(tmp_path: Path) -> None:
    """A [[wikilink]] whose vault_path is missing emits a row with the reason."""
    md = tmp_path / "page.md"
    md.write_text("text [[Acme-Corp]] more", encoding="utf-8")
    rows = audit_mod._broken_link_rows(
        md, md.read_text(encoding="utf-8"), tmp_path, {"Acme-Corp": "02-Areas/Clients/Acme-Corp/"}
    )
    assert len(rows) == 1
    assert rows[0]["link"] == "[[Acme-Corp]]"
    assert "vault_path not found" in rows[0]["reason"]


def test_broken_link_rows_skips_when_target_exists(tmp_path: Path) -> None:
    """If the resolved vault_path exists on disk, no row is emitted."""
    target_dir = tmp_path / "02-Areas" / "Clients" / "Acme-Corp"
    target_dir.mkdir(parents=True)
    md = tmp_path / "page.md"
    md.write_text("text [[Acme-Corp]] more", encoding="utf-8")
    rows = audit_mod._broken_link_rows(
        md, md.read_text(encoding="utf-8"), tmp_path, {"Acme-Corp": "02-Areas/Clients/Acme-Corp/"}
    )
    assert rows == []


def test_broken_link_rows_ignores_untracked_targets(tmp_path: Path) -> None:
    """Wikilinks that don't match any tracked entity vault_path are skipped."""
    md = tmp_path / "page.md"
    md.write_text("[[Random-Link]]", encoding="utf-8")
    rows = audit_mod._broken_link_rows(md, md.read_text(encoding="utf-8"), tmp_path, {"Acme-Corp": "a/"})
    assert rows == []


def test_broken_link_rows_tolerates_trailing_slash(tmp_path: Path) -> None:
    """``vault_path`` stored with a trailing slash matches a file without one."""
    target = tmp_path / "02-Areas" / "Clients" / "Acme-Corp"
    target.mkdir(parents=True)
    md = tmp_path / "page.md"
    md.write_text("[[Acme-Corp]]", encoding="utf-8")
    # Stored path has trailing slash and resolves via ``rstrip("/")``.
    rows = audit_mod._broken_link_rows(
        md, md.read_text(encoding="utf-8"), tmp_path, {"Acme-Corp": "02-Areas/Clients/Acme-Corp/"}
    )
    assert rows == []


# ---------------------------------------------------------------------------
# _gather_audit_files
# ---------------------------------------------------------------------------


def test_gather_audit_files_includes_eligible_under_doc_root(vault_paths, tmp_path: Path) -> None:
    """Eligible markdown under doc_path is returned by the gatherer."""
    doc = Path(vault_paths.document_root)
    knowledge_dir = doc / "04-Agent-Knowledge" / "shared"
    knowledge_dir.mkdir(parents=True)
    page = knowledge_dir / "page.md"
    page.write_text("hello", encoding="utf-8")

    files = audit_mod._gather_audit_files(doc, vault_paths)
    assert page in files


def test_gather_audit_files_excludes_ineligible_paths(vault_paths) -> None:
    """A ``.md`` under an ineligible folder (e.g. archive) is filtered out."""
    doc = Path(vault_paths.document_root)
    bad_dir = doc / "01-Projects" / "archive"
    bad_dir.mkdir(parents=True)
    (bad_dir / "archived.md").write_text("x", encoding="utf-8")

    files = audit_mod._gather_audit_files(doc, vault_paths)
    assert all("archived.md" not in str(f) for f in files)


def test_gather_audit_files_includes_workspace_memory(vault_paths) -> None:
    """Eligible workspace memory files are included in the audit set."""
    ws = Path(vault_paths.workspace_root)
    mem = ws / "agent-alpha" / "memory"
    mem.mkdir(parents=True)
    mem_file = mem / "notes.md"
    mem_file.write_text("note", encoding="utf-8")

    files = audit_mod._gather_audit_files(Path(vault_paths.document_root), vault_paths)
    assert mem_file in files


def test_gather_audit_files_handles_missing_workspace_root(tmp_path: Path) -> None:
    """If workspace_root does not exist on disk, the gather still returns
    the doc-root results without raising."""
    doc = tmp_path / "vault"
    doc.mkdir()
    paths = FakePaths(document_root=doc, workspace_root=tmp_path / "absent")
    knowledge = doc / "04-Agent-Knowledge"
    knowledge.mkdir()
    page = knowledge / "p.md"
    page.write_text("body", encoding="utf-8")

    files = audit_mod._gather_audit_files(doc, paths)
    assert page in files


# ---------------------------------------------------------------------------
# _read_audit_file
# ---------------------------------------------------------------------------


def test_read_audit_file_returns_text_for_normal_file(tmp_path: Path) -> None:
    """A normal small markdown file is returned as its text content."""
    md = tmp_path / "a.md"
    md.write_text("hello world", encoding="utf-8")
    assert audit_mod._read_audit_file(md) == "hello world"


def test_read_audit_file_returns_none_when_oversize(tmp_path: Path, monkeypatch) -> None:
    """A file larger than MAX_FILE_SIZE returns None (skipped)."""
    md = tmp_path / "big.md"
    md.write_text("x", encoding="utf-8")
    # Set MAX_FILE_SIZE smaller than the file by patching the audit-module
    # attribute (module-attribute test seam, not internals import).
    monkeypatch.setattr(audit_mod, "MAX_FILE_SIZE", 0)
    assert audit_mod._read_audit_file(md) is None


def test_read_audit_file_returns_none_for_missing_file(tmp_path: Path) -> None:
    """A missing path returns None — the gatherer skips it silently."""
    assert audit_mod._read_audit_file(tmp_path / "absent.md") is None


# ---------------------------------------------------------------------------
# _already_linked_names
# ---------------------------------------------------------------------------


def test_already_linked_names_collects_plain_targets() -> None:
    """``[[X]]`` populates the linked-names set with ``X``."""
    out = audit_mod._already_linked_names("see [[Acme-Corp]] and [[Bob]]")
    assert "Acme-Corp" in out
    assert "Bob" in out


def test_already_linked_names_collects_alias_pipe_target() -> None:
    """``[[Gamma-Systems|Gamma Systems]]`` adds the canonical target (the
    name before the pipe). The wikilink regex captures only one group so
    the alias-display string is not added to the linked set."""
    out = audit_mod._already_linked_names("[[Gamma-Systems|Gamma Systems]]")
    assert "Gamma-Systems" in out


def test_already_linked_names_empty_for_no_links() -> None:
    """A document with no wikilinks yields an empty set."""
    assert audit_mod._already_linked_names("just plain text") == set()


# ---------------------------------------------------------------------------
# _count_plain_mentions
# ---------------------------------------------------------------------------


def test_count_plain_mentions_counts_whole_word_only() -> None:
    """The helper counts whole-word occurrences and ignores partial matches."""
    e = _entity(name="Bob", aliases=["Bob"])
    content = "Bob talked with Bobby and Bob."
    # 'Bobby' must not be counted; 'Bob' appears twice.
    assert audit_mod._count_plain_mentions(e, content) == 2


def test_count_plain_mentions_is_case_insensitive() -> None:
    """Counts are case-insensitive — 'acme', 'Acme', 'ACME' all match."""
    e = _entity(name="Acme", aliases=["Acme"])
    content = "Acme runs ACME. acme corp."
    assert audit_mod._count_plain_mentions(e, content) == 3


def test_count_plain_mentions_sums_all_triggers() -> None:
    """Every trigger (name + each alias) is counted; the totals sum together."""
    e = _entity(name="Gamma-Systems", aliases=["Gamma-Systems", "Gamma Systems"])
    content = "Gamma-Systems is a company. Gamma Systems also."
    assert audit_mod._count_plain_mentions(e, content) == 2


def test_count_plain_mentions_returns_zero_when_absent() -> None:
    """A trigger that doesn't appear in content scores zero."""
    e = _entity(name="Foo")
    assert audit_mod._count_plain_mentions(e, "completely unrelated") == 0


# ---------------------------------------------------------------------------
# _relative_audit_path
# ---------------------------------------------------------------------------


def test_relative_audit_path_under_doc_root_returns_relative(tmp_path: Path) -> None:
    """A file under the doc root returns the relative path."""
    doc = tmp_path / "vault"
    doc.mkdir()
    f = doc / "sub" / "a.md"
    f.parent.mkdir()
    f.write_text("x", encoding="utf-8")
    out = audit_mod._relative_audit_path(f, doc)
    assert out == "sub/a.md"


def test_relative_audit_path_outside_doc_root_returns_absolute(tmp_path: Path) -> None:
    """A workspace file outside doc_root is returned as its absolute string."""
    doc = tmp_path / "vault"
    doc.mkdir()
    ws = tmp_path / "ws" / "agent" / "memory"
    ws.mkdir(parents=True)
    f = ws / "n.md"
    f.write_text("x", encoding="utf-8")
    out = audit_mod._relative_audit_path(f, doc)
    assert out == str(f)


# ---------------------------------------------------------------------------
# _scan_file_for_unlinked
# ---------------------------------------------------------------------------


def test_scan_file_for_unlinked_emits_row_for_plain_mention(tmp_path: Path) -> None:
    """A plain (unlinked) mention of an entity emits one row with the count."""
    doc = tmp_path / "vault"
    doc.mkdir()
    md = doc / "page.md"
    md.write_text("Acme-Corp is here. Plain text only.", encoding="utf-8")
    entities = [_entity(name="Acme-Corp")]
    rows = audit_mod._scan_file_for_unlinked(md, doc, entities)
    assert len(rows) == 1
    assert rows[0]["entity_name"] == "Acme-Corp"
    assert rows[0]["mention_count"] == 1


def test_scan_file_for_unlinked_skips_when_already_wikilinked(tmp_path: Path) -> None:
    """If every mention is already wrapped in a wikilink, no row is emitted."""
    doc = tmp_path / "vault"
    doc.mkdir()
    md = doc / "page.md"
    md.write_text("see [[Acme-Corp]] only", encoding="utf-8")
    entities = [_entity(name="Acme-Corp")]
    assert audit_mod._scan_file_for_unlinked(md, doc, entities) == []


def test_scan_file_for_unlinked_returns_empty_on_unreadable(tmp_path: Path, monkeypatch) -> None:
    """When _read_audit_file returns None (oversize or unreadable), no rows."""
    doc = tmp_path / "vault"
    doc.mkdir()
    md = doc / "page.md"
    md.write_text("Acme-Corp", encoding="utf-8")
    monkeypatch.setattr(audit_mod, "MAX_FILE_SIZE", 0)
    assert audit_mod._scan_file_for_unlinked(md, doc, [_entity(name="Acme-Corp")]) == []


# ---------------------------------------------------------------------------
# find_unlinked_mentions — public surface; exercises gather + scan together.
# ---------------------------------------------------------------------------


def test_find_unlinked_mentions_sorts_by_mention_count(vault_paths) -> None:
    """Results are sorted descending by ``mention_count`` so the highest-
    impact unlinked entities surface first."""
    doc = Path(vault_paths.document_root)
    knowledge = doc / "04-Agent-Knowledge" / "shared"
    knowledge.mkdir(parents=True)
    (knowledge / "a.md").write_text("Acme Acme Acme Bob", encoding="utf-8")

    entities = [_entity(name="Bob"), _entity(name="Acme")]
    result = audit_mod.find_unlinked_mentions(str(doc), entities, sample_size=10, paths=vault_paths)
    # Acme=3 must appear before Bob=1.
    counts = [(r["entity_name"], r["mention_count"]) for r in result]
    assert counts.index(("Acme", 3)) < counts.index(("Bob", 1))


def test_find_unlinked_mentions_handles_empty_eligible_set(vault_paths) -> None:
    """No eligible files → empty result list."""
    doc = Path(vault_paths.document_root)
    result = audit_mod.find_unlinked_mentions(str(doc), [_entity(name="X")], sample_size=10, paths=vault_paths)
    assert result == []


def test_find_unlinked_mentions_samples_when_oversized(vault_paths) -> None:
    """When eligible count > sample_size, the function samples down to the cap."""
    doc = Path(vault_paths.document_root)
    knowledge = doc / "04-Agent-Knowledge" / "shared"
    knowledge.mkdir(parents=True)
    for i in range(5):
        (knowledge / f"f{i}.md").write_text("Acme", encoding="utf-8")
    entities = [_entity(name="Acme")]
    result = audit_mod.find_unlinked_mentions(str(doc), entities, sample_size=2, paths=vault_paths)
    # At most 2 of 5 eligible files contribute rows — exactly the cap.
    assert len(result) <= 2


# ---------------------------------------------------------------------------
# _read_recent_log — log-reading helper
# ---------------------------------------------------------------------------


def test_read_recent_log_returns_empty_when_log_missing(monkeypatch, tmp_path: Path) -> None:
    """A missing log file returns an empty list (no exception)."""
    monkeypatch.setattr(audit_mod, "_LOG_PATH", str(tmp_path / "absent.jsonl"))
    assert audit_mod._read_recent_log(days=7) == []


def test_read_recent_log_skips_blank_and_bad_lines(monkeypatch, tmp_path: Path) -> None:
    """Blank lines and JSON-decode failures are silently skipped."""
    import time as _time

    log = tmp_path / "log.jsonl"
    now = _time.time()
    body = "\n".join(
        [
            "",
            f'{{"ts": {now}, "injected": ["A"]}}',
            "not-json-at-all",
            f'{{"ts": {now}, "injected": ["B"]}}',
            "",
        ]
    )
    log.write_text(body, encoding="utf-8")
    monkeypatch.setattr(audit_mod, "_LOG_PATH", str(log))
    entries = audit_mod._read_recent_log(days=7)
    assert len(entries) == 2
    assert entries[0]["injected"] == ["A"]


def test_read_recent_log_filters_out_old_entries(monkeypatch, tmp_path: Path) -> None:
    """Entries with ``ts`` older than the cutoff are excluded."""
    import time as _time

    log = tmp_path / "log.jsonl"
    now = _time.time()
    old = now - (30 * 86400)  # 30 days ago
    body = "\n".join(
        [
            f'{{"ts": {old}, "injected": ["OLD"]}}',
            f'{{"ts": {now}, "injected": ["NEW"]}}',
        ]
    )
    log.write_text(body, encoding="utf-8")
    monkeypatch.setattr(audit_mod, "_LOG_PATH", str(log))
    entries = audit_mod._read_recent_log(days=7)
    assert [e["injected"] for e in entries] == [["NEW"]]


# ---------------------------------------------------------------------------
# Markdown rendering — _render_broken_links, _render_unlinked_mentions,
# _render_recent_injections.
# ---------------------------------------------------------------------------


def test_render_broken_links_empty_shows_success() -> None:
    """Empty broken-link list renders a checkmark line."""
    lines = audit_mod._render_broken_links([])
    body = "\n".join(lines)
    assert "No broken links" in body


def test_render_broken_links_populates_table() -> None:
    """A non-empty list builds a markdown table with one row per item."""
    rows = [{"file": "a.md", "link": "[[X]]", "reason": "missing"}]
    lines = audit_mod._render_broken_links(rows)
    body = "\n".join(lines)
    assert "a.md" in body
    assert "[[X]]" in body
    assert "missing" in body
    assert "Found **1** broken" in body


def test_render_broken_links_truncates_long_lists() -> None:
    """A list of >20 items shows the first 20 and an 'and N more' summary."""
    rows = [{"file": f"f{i}.md", "link": "[[X]]", "reason": "r"} for i in range(25)]
    body = "\n".join(audit_mod._render_broken_links(rows))
    assert "and 5 more" in body


def test_render_unlinked_mentions_empty_shows_success() -> None:
    """An empty unlinked-mentions list renders a checkmark."""
    body = "\n".join(audit_mod._render_unlinked_mentions([]))
    assert "No unlinked mentions" in body


def test_render_unlinked_mentions_truncates_long_lists() -> None:
    """A list of >20 unlinked-mention rows truncates with an 'and N more'."""
    rows = [{"file": f"f{i}.md", "entity_name": "X", "mention_count": 1} for i in range(25)]
    body = "\n".join(audit_mod._render_unlinked_mentions(rows))
    assert "and 5 more" in body


def test_render_recent_injections_empty_shows_no_recent() -> None:
    """No log entries → 'No injections' line."""
    body = "\n".join(audit_mod._render_recent_injections([]))
    assert "No injections recorded" in body


def test_render_recent_injections_summarises_runs() -> None:
    """Recent log entries summarise total injections, dry runs, and real runs."""
    entries = [
        {"file": "a.md", "injected": ["A", "B"], "dry_run": False},
        {"file": "b.md", "injected": ["C"], "dry_run": True},
    ]
    body = "\n".join(audit_mod._render_recent_injections(entries))
    assert "Files processed | 2" in body
    assert "Real injections | 1" in body
    assert "Dry runs | 1" in body
    assert "Total wikilinks injected | 3" in body


# ---------------------------------------------------------------------------
# weekly_report — orchestrator wraps the helpers; smoke-test the public surface.
# ---------------------------------------------------------------------------


def test_weekly_report_includes_all_sections(vault_paths, monkeypatch) -> None:
    """The composed report has the entity-ontology table and all three
    section headers, even when each section's data is empty."""
    monkeypatch.setattr(audit_mod, "_LOG_PATH", "/tmp/kairix-audit-test-absent-log.jsonl")
    out = audit_mod.weekly_report(str(vault_paths.document_root), [], paths=vault_paths)
    assert "Wikilink Audit Report" in out
    assert "## Entity Ontology" in out
    assert "## Broken Links" in out
    assert "## Unlinked Mentions" in out
    assert "## Recent Injections" in out


def test_weekly_report_counts_vault_paths(vault_paths, monkeypatch) -> None:
    """The ontology table separates entities with vault_path from those without."""
    monkeypatch.setattr(audit_mod, "_LOG_PATH", "/tmp/kairix-audit-test-absent-log.jsonl")
    entities = [
        _entity(name="X", vault_path="x/"),
        _entity(name="Y", vault_path=""),
    ]
    out = audit_mod.weekly_report(str(vault_paths.document_root), entities, paths=vault_paths)
    assert "Total entities | 2" in out
    assert "With vault_path (linkable) | 1" in out
    assert "Without vault_path (not linked) | 1" in out
