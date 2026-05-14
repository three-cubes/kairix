"""Resolution-order tests for `bundled_suites_root` (#268).

`kairix benchmark list` and `kairix benchmark run <name>` previously
failed inside the container with `No bundled suites found` because the
resolver only consulted `$KAIRIX_SUITES_ROOT` and otherwise returned a
CWD-relative `Path("suites")`. The host-wrapper `docker exec` invocation
lands the process in a working directory that is unrelated to where the
image ships its `suites/` tree.

The fix adds a 4-step resolution chain:

  1. `$KAIRIX_SUITES_ROOT` override
  2. `<repo-root>/suites/` (dev UX)
  3. `/opt/kairix/suites/` (canonical install path)
  4. `./suites/` CWD fallback

These tests drive the pure helper `resolve_first_existing_dir` with
`tmp_path` candidate lists so no env-var monkeypatch is needed (F2-clean).
The wiring of the helper into `bundled_suites_root` itself is exercised
by `tests/test_paths.py::TestShippedAssetPaths::test_bundled_suites_root_env_override`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kairix.paths import resolve_first_existing_dir


@pytest.mark.unit
def test_env_override_wins_even_when_target_missing(tmp_path: Path) -> None:
    """Step 1: the override is returned as-is, even when the path doesn't exist.

    Rationale: a misconfigured `KAIRIX_SUITES_ROOT` should surface as an
    explicit downstream `FileNotFoundError`, not be silently masked by a
    fallback.
    """
    bogus = tmp_path / "nope"  # does NOT exist
    candidate = tmp_path / "would_match"
    candidate.mkdir()

    result = resolve_first_existing_dir(
        override=str(bogus),
        candidates=[candidate],
        fallback=tmp_path / "fallback",
    )

    assert result == bogus


@pytest.mark.unit
def test_empty_override_falls_through_to_candidates(tmp_path: Path) -> None:
    """An empty-string override is treated as no override (env var unset)."""
    repo_root_suites = tmp_path / "repo" / "suites"
    repo_root_suites.mkdir(parents=True)

    result = resolve_first_existing_dir(
        override="",
        candidates=[repo_root_suites],
        fallback=tmp_path / "fallback",
    )

    assert result == repo_root_suites


@pytest.mark.unit
def test_none_override_falls_through_to_candidates(tmp_path: Path) -> None:
    """A None override (env var not set) falls through to the candidate scan."""
    repo_root_suites = tmp_path / "repo" / "suites"
    repo_root_suites.mkdir(parents=True)

    result = resolve_first_existing_dir(
        override=None,
        candidates=[repo_root_suites],
        fallback=tmp_path / "fallback",
    )

    assert result == repo_root_suites


@pytest.mark.unit
def test_first_existing_candidate_wins(tmp_path: Path) -> None:
    """Step 2 (repo-root) beats step 3 (/opt/kairix) when both exist."""
    repo_root_suites = tmp_path / "repo" / "suites"
    repo_root_suites.mkdir(parents=True)
    installed_suites = tmp_path / "opt" / "kairix" / "suites"
    installed_suites.mkdir(parents=True)

    result = resolve_first_existing_dir(
        override=None,
        candidates=[repo_root_suites, installed_suites],
        fallback=tmp_path / "fallback",
    )

    assert result == repo_root_suites


@pytest.mark.unit
def test_second_candidate_used_when_first_missing(tmp_path: Path) -> None:
    """Step 3 (/opt/kairix) wins when step 2 (repo-root) doesn't exist.

    This is the #268 scenario: in the container the dev checkout isn't
    present, so the canonical install path takes over.
    """
    repo_root_suites = tmp_path / "repo" / "suites"  # NOT created
    installed_suites = tmp_path / "opt" / "kairix" / "suites"
    installed_suites.mkdir(parents=True)

    result = resolve_first_existing_dir(
        override=None,
        candidates=[repo_root_suites, installed_suites],
        fallback=tmp_path / "fallback",
    )

    assert result == installed_suites


@pytest.mark.unit
def test_fallback_used_when_no_candidate_exists(tmp_path: Path) -> None:
    """Step 4: when nothing earlier matches, return the CWD-relative fallback."""
    repo_root_suites = tmp_path / "repo" / "suites"  # NOT created
    installed_suites = tmp_path / "opt" / "kairix" / "suites"  # NOT created
    fallback = Path("suites")

    result = resolve_first_existing_dir(
        override=None,
        candidates=[repo_root_suites, installed_suites],
        fallback=fallback,
    )

    assert result == fallback


@pytest.mark.unit
def test_file_at_candidate_path_is_not_used(tmp_path: Path) -> None:
    """A regular file masquerading at the candidate path must not count as a hit.

    Guards against a tar-extraction quirk where ``suites`` ends up as a
    plain file; the resolver must walk past it to the next candidate.
    """
    fake = tmp_path / "suites"
    fake.write_text("not a directory")
    installed_suites = tmp_path / "opt" / "kairix" / "suites"
    installed_suites.mkdir(parents=True)

    result = resolve_first_existing_dir(
        override=None,
        candidates=[fake, installed_suites],
        fallback=tmp_path / "fallback",
    )

    assert result == installed_suites


@pytest.mark.unit
def test_resolution_order_matches_dispatch_brief(tmp_path: Path) -> None:
    """Pin the exact 4-step resolution order from issue #268.

    Constructs all four candidate slots and verifies that each is
    selected in priority order as the higher-priority slots fall away.
    """
    override_dir = tmp_path / "override"
    override_dir.mkdir()
    repo_root_suites = tmp_path / "repo" / "suites"
    repo_root_suites.mkdir(parents=True)
    installed_suites = tmp_path / "opt" / "kairix" / "suites"
    installed_suites.mkdir(parents=True)
    cwd_fallback = tmp_path / "cwd" / "suites"
    cwd_fallback.mkdir(parents=True)

    # Step 1: env-var override wins over everything else.
    assert (
        resolve_first_existing_dir(
            override=str(override_dir),
            candidates=[repo_root_suites, installed_suites],
            fallback=cwd_fallback,
        )
        == override_dir
    )

    # Step 2: repo-root wins when no override.
    assert (
        resolve_first_existing_dir(
            override=None,
            candidates=[repo_root_suites, installed_suites],
            fallback=cwd_fallback,
        )
        == repo_root_suites
    )

    # Step 3: /opt/kairix wins when repo-root is missing.
    missing_repo = tmp_path / "no_repo" / "suites"  # NOT created
    assert (
        resolve_first_existing_dir(
            override=None,
            candidates=[missing_repo, installed_suites],
            fallback=cwd_fallback,
        )
        == installed_suites
    )

    # Step 4: CWD fallback when nothing else exists.
    missing_install = tmp_path / "no_install" / "suites"  # NOT created
    assert (
        resolve_first_existing_dir(
            override=None,
            candidates=[missing_repo, missing_install],
            fallback=cwd_fallback,
        )
        == cwd_fallback
    )
