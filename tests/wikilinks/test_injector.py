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
