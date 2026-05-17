"""
Tests for kairix.knowledge.wikilinks.injector

Covers:
- inject_wikilinks(): first mention, skip second, skip existing, skip code blocks,
  skip frontmatter, whole-word match, own-page skip, aliases
- should_inject(): path eligibility

Path-driven tests consume the ``paths`` / ``test_vault_root`` /
``test_workspaces_root`` fixtures from tests/wikilinks/conftest.py and
inject ``paths=paths`` into the production calls — no env-var monkeypatch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kairix.knowledge.wikilinks.injector import inject_wikilinks, should_inject
from kairix.knowledge.wikilinks.resolver import WikiEntity
from kairix.paths import KairixPaths
from tests.fakes import FakePaths

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def make_entity(
    name: str,
    vault_path: str,
    link: str | None = None,
    aliases: list[str] | None = None,
    entity_type: str = "organisation",
) -> WikiEntity:
    if link is None:
        link = f"[[{name}]]"
    return WikiEntity(
        name=name,
        aliases=aliases or [],
        vault_path=vault_path,
        link=link,
        entity_type=entity_type,
    )


ACME_CORP = make_entity("Acme Corp", "02-Areas/Clients/Acme-Corp/", link="[[Acme-Corp]]")
ACME_CORP_ALT = make_entity("Acme Corp", "02-Areas/Acme Corp/", link="[[AcmeCorp]]")
GAMMA_SYSTEMS = make_entity(
    "Gamma Systems",
    "02-Areas/Clients/Gamma-Systems/",
    link="[[Gamma-Systems|Gamma Systems]]",
    aliases=["Gamma Systems", "BP"],
)


# ---------------------------------------------------------------------------
# inject_wikilinks: first mention, not second
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_injects_on_first_mention() -> None:
    content = "We worked with Acme Corp on their strategy. Acme Corp is a key partner."
    modified, injected = inject_wikilinks(content, [ACME_CORP])
    assert "[[Acme-Corp]]" in modified
    assert injected == ["Acme Corp"]
    # First mention replaced
    assert modified.startswith("We worked with [[Acme-Corp]]")


@pytest.mark.unit
def test_does_not_inject_second_mention() -> None:
    content = "We worked with Acme Corp on their strategy. Acme Corp is a key partner."
    modified, _injected = inject_wikilinks(content, [ACME_CORP])
    # Only one [[Acme-Corp]] in result
    assert modified.count("[[Acme-Corp]]") == 1


# ---------------------------------------------------------------------------
# inject_wikilinks: skip if already a wikilink
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_skips_already_linked() -> None:
    content = "We worked with [[Acme-Corp]] on their strategy."
    modified, injected = inject_wikilinks(content, [ACME_CORP])
    # No change
    assert modified == content
    assert injected == []


@pytest.mark.unit
def test_does_not_double_wrap_wikilink() -> None:
    # Use an entity whose name matches its link slug exactly (no space → slug differs).
    # When the link target and display name are identical, _find_already_linked() correctly
    # marks the entity as already linked and suppresses injection on subsequent mentions.
    simple = make_entity("Softcorp", "02-Areas/Work/Orgs/Softcorp/")  # link="[[Softcorp]]"
    content = "[[Softcorp]] is an org. Softcorp also does software."
    modified, injected = inject_wikilinks(content, [simple])
    # No new injection — Softcorp already linked on first occurrence
    assert modified.count("[[Softcorp]]") == 1
    assert injected == []


# ---------------------------------------------------------------------------
# inject_wikilinks: skip inside code blocks
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_skips_fenced_code_block() -> None:
    content = (
        "Here is code:\n\n```python\n# Acme Corp client\nclient = 'Acme Corp'\n```\n\nAcme Corp is an organisation."
    )
    modified, _injected = inject_wikilinks(content, [ACME_CORP])
    # Should inject on 'Acme Corp is an organisation' (after the code block), not inside it
    assert "[[Acme-Corp]]" in modified
    # The Acme Corp inside the code block should remain unlinked
    assert "```python\n# Acme Corp client" in modified or "```python\n# [[Acme-Corp]] client" not in modified


@pytest.mark.unit
def test_skips_inline_code() -> None:
    content = "Use the `Acme Corp` constant. Acme Corp is our company."
    modified, injected = inject_wikilinks(content, [ACME_CORP])
    # The body occurrence of Acme Corp (outside backticks) is linked
    assert "[[Acme-Corp]]" in modified
    # The inline code occurrence is NOT wrapped in a link
    assert "`[[Acme-Corp]]`" not in modified
    assert injected == ["Acme Corp"]


# ---------------------------------------------------------------------------
# inject_wikilinks: skip frontmatter
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_skips_frontmatter() -> None:
    content = "---\ntitle: Acme Corp Project\nclient: Acme Corp\n---\n\nAcme Corp is a major health insurer."
    modified, injected = inject_wikilinks(content, [ACME_CORP])
    # Should inject in body, not frontmatter
    assert injected == ["Acme Corp"]
    # Frontmatter preserved intact
    assert "---\ntitle: Acme Corp Project\nclient: Acme Corp\n---" in modified
    # Body mention linked
    assert "[[Acme-Corp]] is a major health insurer." in modified


@pytest.mark.unit
def test_frontmatter_acme_not_linked_in_yaml() -> None:
    content = "---\nclient: Acme Corp\n---\n\nAcme Corp overview."
    modified, _ = inject_wikilinks(content, [ACME_CORP])
    # frontmatter Acme Corp stays as-is
    assert "client: [[Acme-Corp]]" not in modified
    assert "client: Acme Corp" in modified


# ---------------------------------------------------------------------------
# inject_wikilinks: whole-word match only
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_whole_word_match_only() -> None:
    content = "Acme-CorpGroup is not the same as Acme Corp."
    modified, injected = inject_wikilinks(content, [ACME_CORP])
    # "Acme-CorpGroup" should NOT be linked
    assert "[[Acme-Corp]]Group" not in modified
    # "Acme Corp" at end should be linked
    assert "[[Acme-Corp]]." in modified
    assert injected == ["Acme Corp"]


@pytest.mark.unit
def test_no_match_for_substring() -> None:
    content = "Acme-CorpGroup and SubAcme-Corp are different."
    modified, injected = inject_wikilinks(content, [ACME_CORP])
    # Neither "Acme-CorpGroup" nor "SubAcme-Corp" are whole-word matches for "Acme Corp"
    assert injected == []
    assert "[[Acme-Corp]]" not in modified


# ---------------------------------------------------------------------------
# inject_wikilinks: don't inject entity on its own page
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_no_self_link_on_own_page(paths: KairixPaths, test_vault_root: str) -> None:
    content = "Acme Corp is a major health insurer with global operations."
    modified, injected = inject_wikilinks(
        content,
        [ACME_CORP],
        source_path=f"{test_vault_root}/02-Areas/Clients/Acme-Corp/Overview.md",
        paths=paths,
    )
    assert injected == []
    assert "[[Acme-Corp]]" not in modified


@pytest.mark.unit
def test_self_link_check_different_entity(paths: KairixPaths, test_vault_root: str) -> None:
    """On Acme Corp's page, a different entity is still linked."""
    content = "Acme Corp works with Gamma Systems on strategy."
    modified, injected = inject_wikilinks(
        content,
        [ACME_CORP, GAMMA_SYSTEMS],
        source_path=f"{test_vault_root}/02-Areas/Clients/Acme-Corp/Overview.md",
        paths=paths,
    )
    # Acme Corp suppressed (self-link on own page); Gamma Systems still linked
    assert "[[Acme-Corp]]" not in modified
    assert "[[Gamma-Systems|Gamma Systems]]" in modified
    assert "Gamma Systems" in injected
    assert "Acme Corp" not in injected


# ---------------------------------------------------------------------------
# inject_wikilinks: aliases
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_alias_triggers_link() -> None:
    """Alias 'Gamma Systems' should trigger the [[Gamma-Systems|Gamma Systems]] link."""
    content = "Gamma Systems is a fast food chain."
    modified, injected = inject_wikilinks(content, [GAMMA_SYSTEMS])
    assert "[[Gamma-Systems|Gamma Systems]]" in modified
    assert injected == ["Gamma Systems"]


@pytest.mark.unit
def test_primary_name_triggers_link() -> None:
    """Primary name 'Gamma Systems' should also trigger."""
    content = "Gamma Systems is a fast food chain."
    modified, injected = inject_wikilinks(content, [GAMMA_SYSTEMS])
    assert "[[Gamma-Systems|Gamma Systems]]" in modified
    assert injected == ["Gamma Systems"]


# ---------------------------------------------------------------------------
# should_inject: eligibility
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_should_inject_memory_log(paths: KairixPaths, test_workspaces_root: str) -> None:
    assert should_inject(f"{test_workspaces_root}/builder/memory/2026-03-23.md", paths=paths) is True


@pytest.mark.unit
def test_should_inject_agent_knowledge(paths: KairixPaths, test_vault_root: str) -> None:
    assert should_inject(f"{test_vault_root}/04-Agent-Knowledge/builder/patterns.md", paths=paths) is True


@pytest.mark.unit
def test_should_inject_projects(paths: KairixPaths, test_vault_root: str) -> None:
    assert should_inject(f"{test_vault_root}/01-Projects/202603-Kairix/README.md", paths=paths) is True


@pytest.mark.unit
def test_should_inject_areas(paths: KairixPaths, test_vault_root: str) -> None:
    assert should_inject(f"{test_vault_root}/02-Areas/Clients/Acme-Corp/Overview.md", paths=paths) is True


@pytest.mark.unit
def test_should_inject_knowledge(paths: KairixPaths, test_vault_root: str) -> None:
    assert should_inject(f"{test_vault_root}/05-Knowledge/01-Strategy/notes.md", paths=paths) is True


@pytest.mark.unit
def test_should_not_inject_archived_path(paths: KairixPaths) -> None:
    assert should_inject("/vault/02-Areas/Clients/Acme-Corp/archive/old.md", paths=paths) is False


@pytest.mark.unit
def test_should_not_inject_archived_substring(paths: KairixPaths) -> None:
    assert should_inject("/vault/archived/2023/something.md", paths=paths) is False


@pytest.mark.unit
def test_should_not_inject_shape_cache(paths: KairixPaths) -> None:
    assert should_inject("/home/<service-user>/.cache/shape/some-import.md", paths=paths) is False


@pytest.mark.unit
def test_should_not_inject_non_md(paths: KairixPaths, test_workspaces_root: str) -> None:
    assert should_inject(f"{test_workspaces_root}/builder/memory/notes.txt", paths=paths) is False


@pytest.mark.unit
def test_should_not_inject_workspace_non_memory(paths: KairixPaths, test_workspaces_root: str) -> None:
    """Workspace files outside /memory/ subfolder should NOT be eligible."""
    assert should_inject(f"{test_workspaces_root}/builder/some-other-dir/notes.md", paths=paths) is False


@pytest.mark.unit
def test_should_not_inject_large_file(tmp_path: Path, paths: KairixPaths) -> None:
    """Files > 500KB should not be eligible.

    We exercise the size check via ``inject_file`` against a real >500KB file
    in ``tmp_path``. The path won't match eligible prefixes, but ``inject_file``
    short-circuits on size before consulting the prefix list, so the test
    still proves the size guard fires.
    """
    large_file = tmp_path / "big.md"
    large_file.write_bytes(b"x" * (501 * 1024))

    from kairix.knowledge.wikilinks.injector import inject_file

    result = inject_file(str(large_file), [ACME_CORP], paths=paths)
    assert result == []


# ---------------------------------------------------------------------------
# Alias normalisation: alias surface form → canonical [[link]]
# ---------------------------------------------------------------------------

# WikiEntity for Delta Co that has BWE-C and BWE&C as aliases
BRIDGEWATER = make_entity(
    "Delta Co",
    "06-Entities/concept/bridgewater-engineering.md",
    link="[[Delta-Co]]",
    aliases=["BWE-C", "BWE&C"],
    entity_type="concept",
)


@pytest.mark.unit
def test_alias_surface_form_produces_canonical_link() -> None:
    """'BWE&C strategy' → '[[Delta-Co]] strategy' (alias triggers canonical link)."""
    content = "The BWE&C strategy is evolving."
    modified, injected = inject_wikilinks(content, [BRIDGEWATER])
    assert "[[Delta-Co]]" in modified, f"Expected [[Delta-Co]] in: {modified}"
    assert "[[BWE&C]]" not in modified
    assert injected == ["Delta Co"]


@pytest.mark.unit
def test_alias_sme_c_produces_canonical_link() -> None:
    """'BWE-C' surface form → '[[Delta-Co]]'."""
    content = "BWE-C is a well-known company."
    modified, injected = inject_wikilinks(content, [BRIDGEWATER])
    assert "[[Delta-Co]]" in modified
    assert injected == ["Delta Co"]


@pytest.mark.unit
def test_canonical_name_still_works_with_aliases_defined() -> None:
    """Primary name 'Delta Co' still triggers '[[Delta-Co]]' even when aliases exist."""
    content = "Delta Co is a major infrastructure company."
    modified, injected = inject_wikilinks(content, [BRIDGEWATER])
    assert "[[Delta-Co]]" in modified
    assert injected == ["Delta Co"]


@pytest.mark.unit
def test_only_first_alias_mention_linked() -> None:
    """Only the first occurrence of any alias form is linked."""
    content = "BWE&C works on big projects. BWE-C is part of the same group. Delta Co is canonical."
    modified, injected = inject_wikilinks(content, [BRIDGEWATER])
    # Only one [[Delta-Co]] should appear
    assert modified.count("[[Delta-Co]]") == 1
    assert injected == ["Delta Co"]


@pytest.mark.unit
def test_gamma_systems_alias_produces_canonical_link() -> None:
    """'Gamma Systems' → '[[Gamma-Systems|Gamma Systems]]' (alias → canonical display)."""
    content = "Gamma Systems is a major fast food chain."
    modified, injected = inject_wikilinks(content, [GAMMA_SYSTEMS])
    assert "[[Gamma-Systems|Gamma Systems]]" in modified
    assert injected == ["Gamma Systems"]


# ---------------------------------------------------------------------------
# should_inject — explicit rejection branches
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_should_not_inject_oversized_file_at_eligible_prefix(
    tmp_path: Path, paths: KairixPaths, test_vault_root: str
) -> None:
    """A file under an eligible prefix that exceeds MAX_FILE_SIZE is rejected.

    Drives the ``return False`` after ``os.path.getsize > MAX_FILE_SIZE`` in
    ``should_inject`` (line 98) directly, rather than transitively through
    ``inject_file`` (which short-circuits earlier on a different branch).
    """
    # Place a >500KB file at an eligible vault prefix path.
    path = tmp_path / "vault" / "02-Areas" / "big.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"x" * (501 * 1024))

    eligible_paths = FakePaths(
        document_root=str(tmp_path / "vault"),
        workspace_root=str(tmp_path / "workspaces"),
    )
    assert should_inject(str(path), paths=eligible_paths) is False


@pytest.mark.unit
def test_should_not_inject_path_outside_eligible_prefixes(paths: KairixPaths, test_vault_root: str) -> None:
    """A .md file outside every eligible prefix returns False.

    Closes coverage of the final ``return False`` after the prefix loop
    (line 118) — distinct from the workspace-non-memory branch above it.
    """
    # Path is not the workspace root, not the document root — fully outside.
    assert should_inject("/some/other/place/notes.md", paths=paths) is False


# ---------------------------------------------------------------------------
# inject_wikilinks — own-page skip with non-doc-root source path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_inject_skips_own_page_when_source_path_is_relative() -> None:
    """A relative source_path (not under document_root) is matched against entity vault_path verbatim.

    Closes coverage of the ``rel = source_path`` branch at line 196 — when
    source_path doesn't start with the doc-root prefix, it's used as-is for
    the own-page comparison.
    """
    entity = make_entity("Acme", "02-Areas/Clients/Acme/Acme.md", link="[[Acme]]")
    content = "Acme is the client."
    paths_obj = FakePaths(
        document_root="/var/lib/kairix-test/vault",
        workspace_root="/var/lib/kairix-test/workspaces",
    )

    # source_path is just the relative path — no doc-root prefix.
    modified, injected = inject_wikilinks(
        content,
        [entity],
        source_path="02-Areas/Clients/Acme/Acme.md",
        paths=paths_obj,
    )
    # The entity is on its own page → no link is injected.
    assert injected == []
    assert modified == content


# ---------------------------------------------------------------------------
# _find_already_linked — alias display name branch
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_existing_aliased_wikilink_blocks_injection_under_display_name() -> None:
    """An existing ``[[target|display]]`` wikilink registers BOTH ``target`` and ``display``
    as already-linked, so a subsequent mention of ``display`` is not re-linked.

    Closes coverage of ``linked.add(display)`` at line 217 inside
    ``_find_already_linked``.
    """
    # The content already contains the aliased form Gamma-Systems with display "Gamma Systems".
    # A subsequent free-text mention of "Gamma Systems" must not be re-linked.
    content = "We met [[Gamma-Systems|Gamma Systems]] at lunch. Later Gamma Systems sent a follow-up."
    modified, injected = inject_wikilinks(content, [GAMMA_SYSTEMS])
    assert injected == [], f"expected no injection but got: {injected}; modified: {modified}"
    # The free-text "Gamma Systems" remains unwrapped — proving the alias was recorded.
    assert "Later Gamma Systems sent" in modified


# ---------------------------------------------------------------------------
# _parse_segments — unclosed fenced code block
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_unclosed_fenced_code_block_is_treated_as_code_to_end_of_file() -> None:
    """Content with an opening ``` but no closing fence treats the rest as code.

    Closes coverage of the ``segments.append(("fenced_code", ...))`` and the
    ``pos = n`` short-circuit at lines 262-263. Without that, an unclosed
    fence would let the rest of the file leak into a "text" segment and get
    wikilink-injected.
    """
    content = "Acme Corp is the client.\n```python\nstill in code\nAcme Corp again\n"  # no closing ```
    modified, injected = inject_wikilinks(content, [ACME_CORP])
    # First "Acme Corp" (in text) IS linked.
    assert injected == ["Acme Corp"]
    # Second "Acme Corp" (inside the unclosed code block) is NOT linked.
    code_section = modified.split("```python", 1)[1]
    assert "[[Acme-Corp]]" not in code_section, (
        f"unclosed code fence leaked into text injection; code section: {code_section!r}"
    )


# ---------------------------------------------------------------------------
# _is_in_code_or_link — cursor inside an open [[ ... region
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_match_inside_unclosed_double_bracket_is_skipped() -> None:
    """A trigger inside an unclosed ``[[...`` (no matching ``]]``) is not re-wrapped.

    Closes coverage of the ``return True`` branch in ``_is_in_code_or_link``
    at lines 356-359 — when the cursor is between an open ``[[`` and there is
    no closing ``]]`` *before* the cursor. This is the "malformed/unclosed
    wikilink" defensive guard.

    Construction: ``[[Note: about Acme Corp behaviour`` — the ``[[`` opens but
    is never closed. The pre-scan (_find_already_linked) only matches well-formed
    ``[[...]]`` so does NOT register Acme Corp as already-linked, leaving
    _is_in_code_or_link as the gate.
    """
    content = "Random ramble [[Note: about Acme Corp behaviour"
    modified, injected = inject_wikilinks(content, [ACME_CORP])
    # The malformed [[ is preserved unchanged; Acme Corp inside it is not wrapped.
    assert injected == [], f"unexpected injection: {injected}; modified: {modified!r}"
    assert modified == content, f"content mutated unexpectedly: {modified!r}"


# ---------------------------------------------------------------------------
# inject_file — non-md, stat error, read failure, dry-run, log path
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_inject_file_returns_empty_for_non_markdown_extension(tmp_path: Path, paths: KairixPaths) -> None:
    """A .txt file is silently skipped (returns []) without reading."""
    from kairix.knowledge.wikilinks.injector import inject_file

    txt = tmp_path / "notes.txt"
    txt.write_text("Acme Corp", encoding="utf-8")
    result = inject_file(str(txt), [ACME_CORP], paths=paths)
    assert result == []
    # The file was not modified.
    assert txt.read_text(encoding="utf-8") == "Acme Corp"


@pytest.mark.unit
def test_inject_file_returns_empty_when_stat_fails(tmp_path: Path, paths: KairixPaths) -> None:
    """A path that doesn't exist returns [] (the OSError on stat is caught)."""
    from kairix.knowledge.wikilinks.injector import inject_file

    missing = tmp_path / "does-not-exist.md"
    result = inject_file(str(missing), [ACME_CORP], paths=paths)
    assert result == []


@pytest.mark.unit
def test_inject_file_returns_empty_when_content_is_invalid_utf8(tmp_path: Path, paths: KairixPaths) -> None:
    """A .md file with non-UTF-8 bytes is silently skipped (UnicodeDecodeError caught)."""
    from kairix.knowledge.wikilinks.injector import inject_file

    bad = tmp_path / "binary.md"
    bad.write_bytes(b"\xff\xfe\xfd not utf-8")
    result = inject_file(str(bad), [ACME_CORP], paths=paths)
    assert result == []


@pytest.mark.unit
def test_inject_file_writes_modified_content_when_injection_succeeds(tmp_path: Path, paths: KairixPaths) -> None:
    """When entities are injected and dry_run=False, the modified content is written back."""
    from kairix.knowledge.wikilinks.injector import inject_file

    md = tmp_path / "page.md"
    md.write_text("Acme Corp is here.", encoding="utf-8")
    result = inject_file(str(md), [ACME_CORP], dry_run=False, paths=paths)
    assert result == ["Acme Corp"]
    after = md.read_text(encoding="utf-8")
    assert "[[Acme-Corp]]" in after, f"expected wikilink in file; got: {after!r}"


@pytest.mark.unit
def test_inject_file_does_not_write_when_dry_run(tmp_path: Path, paths: KairixPaths) -> None:
    """dry_run=True returns the injected names but leaves the file untouched."""
    from kairix.knowledge.wikilinks.injector import inject_file

    md = tmp_path / "page.md"
    original = "Acme Corp is here."
    md.write_text(original, encoding="utf-8")
    result = inject_file(str(md), [ACME_CORP], dry_run=True, paths=paths)
    assert result == ["Acme Corp"]
    assert md.read_text(encoding="utf-8") == original


@pytest.mark.unit
def test_inject_file_returns_empty_when_no_entity_matches(tmp_path: Path, paths: KairixPaths) -> None:
    """When no entity trigger matches the content, injection list is empty and file is untouched."""
    from kairix.knowledge.wikilinks.injector import inject_file

    md = tmp_path / "page.md"
    md.write_text("This document mentions nothing relevant.", encoding="utf-8")
    result = inject_file(str(md), [ACME_CORP], paths=paths)
    assert result == []
    assert md.read_text(encoding="utf-8") == "This document mentions nothing relevant."


@pytest.mark.unit
def test_inject_log_strips_doc_root_prefix_from_file_path(tmp_path: Path) -> None:
    """When the injected file lives under ``paths.document_root``, the log entry stores
    the relative document path (not the absolute filesystem path).

    Closes coverage of the doc-root-stripping branch in ``_log_injection`` (line 435).
    """
    from kairix.knowledge.wikilinks.injector import inject_file

    doc_root = tmp_path / "vault"
    doc_root.mkdir()
    md = doc_root / "02-Areas" / "page.md"
    md.parent.mkdir(parents=True)
    md.write_text("Acme Corp is here.", encoding="utf-8")

    log_path = tmp_path / "log.jsonl"
    paths_obj = FakePaths(
        document_root=str(doc_root),
        workspace_root=str(tmp_path / "workspaces"),
    )

    result = inject_file(str(md), [ACME_CORP], paths=paths_obj, log_path=log_path)
    assert result == ["Acme Corp"]
    # Log entry's `file` field is the path with the doc_root prefix stripped.
    import json as _json

    entries = [_json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line]
    assert len(entries) == 1
    assert entries[0]["file"] == "02-Areas/page.md", (
        f"expected stripped path '02-Areas/page.md'; got: {entries[0]['file']!r}"
    )
    assert entries[0]["injected"] == ["Acme Corp"]
    assert entries[0]["dry_run"] is False


@pytest.mark.unit
def test_inject_log_swallows_os_error_when_log_path_unwritable(tmp_path: Path, paths: KairixPaths) -> None:
    """A failing log write must not abort the injection (logging is non-fatal).

    Closes coverage of the ``except OSError: pass`` branch in ``_log_injection``
    (lines 448-449). We point ``log_path`` at a directory — ``open("a")``
    raises IsADirectoryError (an OSError subclass) so the except branch fires.
    The injection itself still succeeds.
    """
    from kairix.knowledge.wikilinks.injector import inject_file

    md = tmp_path / "page.md"
    md.write_text("Acme Corp lives here.", encoding="utf-8")

    # The log_path is a directory — opening it for append raises IsADirectoryError.
    bad_log = tmp_path / "log_dir"
    bad_log.mkdir()

    result = inject_file(str(md), [ACME_CORP], paths=paths, log_path=bad_log)
    # Injection succeeded despite log failure.
    assert result == ["Acme Corp"]
    after = md.read_text(encoding="utf-8")
    assert "[[Acme-Corp]]" in after
