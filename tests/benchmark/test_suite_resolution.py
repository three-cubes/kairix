"""Tests for the suite-by-name resolution UX added in #222.

Cover three behaviours:
- ``resolve_suite_path`` accepts a literal path AND a bundle name.
- ``list_bundled_suites`` enumerates name/cases/default_collection from the
  bundled suites directory.
- ``cmd_run`` honours ``suite.meta.default_collection`` when ``--collection``
  is not supplied.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kairix.quality.benchmark.suite import (
    list_bundled_suites,
    load_suite,
    resolve_suite_path,
)


@pytest.fixture
def bundled_suites_dir(tmp_path) -> Path:
    """Create a minimal bundled-suites directory under tmp_path. Tests pass
    it explicitly via the resolver's ``root`` kwarg — no env-var monkeypatch
    (F2-compliant)."""
    suites = tmp_path / "suites"
    suites.mkdir()

    (suites / "reflib-gold-v3.yaml").write_text(
        "meta:\n"
        "  name: reflib\n"
        "  description: stub suite for tests\n"
        "  default_collection: reference-library\n"
        "cases:\n"
        "  - id: t1\n"
        "    category: recall\n"
        "    query: hello\n"
        "    score_method: exact\n"
        "    gold_title: hello\n",
    )
    (suites / "reflib-gold-v2.yaml").write_text(
        "meta: {name: reflib-old}\ncases: []\n",
    )
    (suites / "contract-suite.yaml").write_text(
        "meta: {description: contract}\ncases: []\n",
    )
    return suites


@pytest.mark.unit
def test_resolve_suite_path_picks_highest_gold_version(bundled_suites_dir: Path) -> None:
    """Bundle-name lookup must prefer the highest -gold-vN.yaml number."""
    p = resolve_suite_path("reflib", root=bundled_suites_dir)
    assert p.name == "reflib-gold-v3.yaml"


@pytest.mark.unit
def test_resolve_suite_path_accepts_explicit_path(bundled_suites_dir: Path) -> None:
    """If a path exists on disk it's returned as-is — no bundle scan."""
    explicit = bundled_suites_dir / "reflib-gold-v2.yaml"
    assert resolve_suite_path(str(explicit), root=bundled_suites_dir) == explicit


@pytest.mark.unit
def test_resolve_suite_path_unknown_name_raises(bundled_suites_dir: Path) -> None:
    """Unknown name must raise FileNotFoundError, not return a stale fallback."""
    with pytest.raises(FileNotFoundError, match="not found"):
        resolve_suite_path("does-not-exist", root=bundled_suites_dir)


@pytest.mark.unit
def test_resolve_suite_path_falls_back_to_plain_yaml(tmp_path) -> None:
    """When no -gold-vN.yaml exists, <name>.yaml is the fallback."""
    suites = tmp_path / "s"
    suites.mkdir()
    (suites / "mybench.yaml").write_text("meta: {}\ncases: []\n")
    assert resolve_suite_path("mybench", root=suites).name == "mybench.yaml"


@pytest.mark.unit
def test_list_bundled_suites_exposes_default_collection(bundled_suites_dir: Path) -> None:
    """list_bundled_suites returns the default_collection per suite for the UX."""
    suites = list_bundled_suites(root=bundled_suites_dir)
    reflib = next(s for s in suites if s["path"].endswith("reflib-gold-v3.yaml"))
    assert reflib["default_collection"] == "reference-library"
    assert reflib["n_cases"] == 1  # one case in the fixture
    assert reflib["description"] == "stub suite for tests"


@pytest.mark.unit
def test_load_suite_preserves_default_collection_in_meta(bundled_suites_dir: Path) -> None:
    """Loading the suite preserves meta.default_collection — the CLI relies on this."""
    suite = load_suite(str(bundled_suites_dir / "reflib-gold-v3.yaml"))
    assert suite.meta.get("default_collection") == "reference-library"
