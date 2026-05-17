"""Integration tests for layered config loading — base + overlay end-to-end.

TDD-first: writes real YAML files on disk, drives ``load_config`` through
its public surface, asserts the parsed ``RetrievalConfig`` reflects the
merge semantics described in
``tests/bdd/features/config_layering.feature``.

These tests sit above :mod:`tests.core.search.test_config_overlay` (which
unit-tests the merge primitives) and below the BDD feature (which is
operator-language). Together the three layers pin the contract: unit
proves the algebra, integration proves the I/O wiring, BDD proves the
operator-visible semantics.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kairix.core.search.config_loader import (
    ConfigValidationError,
    load_config,
)

pytestmark = pytest.mark.integration


_BASE_YAML = """
_schema_version: 1
provider: azure_foundry

retrieval:
  fusion_strategy: bm25_primary
  rrf_k: 60
  vec_limit: 10
  boosts:
    entity:
      enabled: true
      factor: 0.20
      cap: 2.0
    procedural:
      enabled: true
      factor: 1.4
"""


def _write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Base alone — operator hasn't set an overlay
# ---------------------------------------------------------------------------


def test_base_only_resolves_required_provider_key(tmp_path: Path) -> None:
    """When no overlay is set, the base alone produces a valid RetrievalConfig
    with the bundled provider value."""
    base = _write(tmp_path / "base.yaml", _BASE_YAML)
    env = {"KAIRIX_CONFIG_BASE_PATH": str(base)}

    cfg = load_config(env=env)

    assert cfg.provider == "azure_foundry"
    assert cfg.fusion_strategy == "bm25_primary"
    assert cfg.entity.factor == 0.20


# ---------------------------------------------------------------------------
# Overlay overrides — sparse operator file
# ---------------------------------------------------------------------------


def test_overlay_provider_overrides_base(tmp_path: Path) -> None:
    """Overlay's `provider:` wins over base's `provider:`."""
    base = _write(tmp_path / "base.yaml", _BASE_YAML)
    overlay = _write(tmp_path / "overlay.yaml", "provider: ollama\n")
    env = {
        "KAIRIX_CONFIG_BASE_PATH": str(base),
        "KAIRIX_CONFIG_OVERLAY_PATH": str(overlay),
    }

    cfg = load_config(env=env)

    assert cfg.provider == "ollama"


def test_overlay_without_provider_inherits_from_base(tmp_path: Path) -> None:
    """Sparse overlay that doesn't mention `provider:` inherits the base's
    value — the failure mode that bit v2026.5.17a9 on the alpha host.

    Sabotage: revert _deep_merge to ``overlay`` (overlay-only) and the
    overlay's missing provider blanks out the base's → ConfigValidationError
    downstream.
    """
    base = _write(tmp_path / "base.yaml", _BASE_YAML)
    overlay = _write(
        tmp_path / "overlay.yaml",
        "retrieval:\n  rrf_k: 10\n",
    )
    env = {
        "KAIRIX_CONFIG_BASE_PATH": str(base),
        "KAIRIX_CONFIG_OVERLAY_PATH": str(overlay),
    }

    cfg = load_config(env=env)

    # Provider inherited from base.
    assert cfg.provider == "azure_foundry"
    # Overlay's rrf_k wins.
    assert cfg.rrf_k == 10
    # Base's other retrieval defaults survive.
    assert cfg.fusion_strategy == "bm25_primary"


def test_overlay_collections_replace_base_collections(tmp_path: Path) -> None:
    """Lists are replaced, not concatenated — operator declaring their own
    `collections.shared` gets exactly their list."""
    base = _write(
        tmp_path / "base.yaml",
        """
_schema_version: 1
provider: azure_foundry
collections:
  shared:
    - name: home
      path: 00-Home
    - name: projects
      path: 01-Projects
    - name: areas
      path: 02-Areas
""",
    )
    overlay = _write(
        tmp_path / "overlay.yaml",
        """
collections:
  shared:
    - name: vault-only
      path: vault
""",
    )
    env = {
        "KAIRIX_CONFIG_BASE_PATH": str(base),
        "KAIRIX_CONFIG_OVERLAY_PATH": str(overlay),
    }

    # Load via a stable path that surfaces the merged collections-section.
    from kairix.core.search.config_loader import load_layered_yaml

    merged = load_layered_yaml(env=env)
    shared = merged.get("collections", {}).get("shared", [])
    assert len(shared) == 1
    assert shared[0]["name"] == "vault-only"


# ---------------------------------------------------------------------------
# Schema mismatch — refuse with actionable error
# ---------------------------------------------------------------------------


def test_schema_version_mismatch_raises_actionable_error(tmp_path: Path) -> None:
    """Overlay declares it needs schema ≥2 but base ships v1 → refuse loudly.

    The error must guide the operator to either upgrade the image (so
    base ships v2) or drop the overlay's `_schema_version_required_min`
    if they've manually verified compatibility.
    """
    base = _write(tmp_path / "base.yaml", "_schema_version: 1\nprovider: azure_foundry\n")
    overlay = _write(
        tmp_path / "overlay.yaml",
        "_schema_version_required_min: 2\nprovider: ollama\n",
    )
    env = {
        "KAIRIX_CONFIG_BASE_PATH": str(base),
        "KAIRIX_CONFIG_OVERLAY_PATH": str(overlay),
    }

    with pytest.raises(ConfigValidationError) as info:
        load_config(env=env)

    msg = str(info.value)
    assert "_schema_version" in msg
    assert "fix:" in msg or "next:" in msg or "run:" in msg, "operator-facing message must carry an F21 action marker"


# ---------------------------------------------------------------------------
# Legacy single-file path
# ---------------------------------------------------------------------------


def test_legacy_single_file_path_still_works(tmp_path: Path) -> None:
    """`KAIRIX_CONFIG_PATH` without overlay → single-file mode (backward compat)."""
    single = _write(
        tmp_path / "legacy.yaml",
        """
provider: bedrock
retrieval:
  fusion_strategy: rrf
  rrf_k: 30
""",
    )
    env = {"KAIRIX_CONFIG_PATH": str(single)}

    cfg = load_config(env=env)

    assert cfg.provider == "bedrock"
    assert cfg.fusion_strategy == "rrf"
    assert cfg.rrf_k == 30
