"""Unit tests for the layered config loader — base + sparse operator overlay.

TDD-first: these tests drive the implementation of
``kairix.core.search.config_loader`` overlay support. The contract:

  - ``deep_merge`` does dict-recursive + scalar/list-replace
  - ``resolve_layered_paths`` honours the env-var resolution matrix
  - ``validate_schema_compat`` refuses overlay+base version mismatches
  - the merge respects the operator-intent: "I'm overriding *these* keys;
    everything else stays at the image-bundled default"

All tests sit on the public surfaces; no monkey-patching of internals
(F1-clean). Env-var inputs flow through the resolver's explicit kwargs
(F2-clean — no ``monkeypatch.setenv``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kairix.core.search.config_loader import (
    ConfigValidationError,
    deep_merge,
    resolve_layered_paths,
    validate_schema_compat,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# deep_merge — the core overlay-on-base operation
# ---------------------------------------------------------------------------


def test_deep_merge_dict_into_dict_is_recursive() -> None:
    """Nested dicts merge recursively — overlay leaves don't shadow base siblings.

    Sabotage: make deep_merge replace dicts wholesale instead of recursing
    and base['retrieval']['fusion_strategy'] disappears when overlay sets
    only retrieval.boosts.entity.factor.
    """
    base = {"retrieval": {"fusion_strategy": "bm25_primary", "boosts": {"entity": {"factor": 0.20}}}}
    overlay = {"retrieval": {"boosts": {"entity": {"factor": 0.50}}}}

    result = deep_merge(base, overlay)

    # Overlay leaf wins.
    assert result["retrieval"]["boosts"]["entity"]["factor"] == 0.50
    # Base sibling at the OUTER level survives.
    assert result["retrieval"]["fusion_strategy"] == "bm25_primary"


def test_deep_merge_overlay_scalar_replaces_base_scalar() -> None:
    """Overlay scalars override base scalars."""
    base = {"provider": "azure_foundry"}
    overlay = {"provider": "ollama"}

    result = deep_merge(base, overlay)

    assert result["provider"] == "ollama"


def test_deep_merge_overlay_list_replaces_base_list() -> None:
    """Overlay lists REPLACE base lists wholesale — no concat.

    Operator intent: ``collections.shared: [{name: ...}, ...]`` in the
    overlay declares "my full list, not an addition". Concat would
    silently keep the image's vault paths present even when the operator
    is running a different vault layout.

    Sabotage: change deep_merge to extend the list and a 2-item
    overlay collection list becomes 9 (base 7 + overlay 2).
    """
    base = {"collections": {"shared": [{"name": "a"}, {"name": "b"}, {"name": "c"}]}}
    overlay = {"collections": {"shared": [{"name": "z"}]}}

    result = deep_merge(base, overlay)

    assert result["collections"]["shared"] == [{"name": "z"}]


def test_deep_merge_empty_overlay_returns_base_unchanged() -> None:
    """Empty overlay is a no-op."""
    base = {"provider": "azure_foundry", "retrieval": {"rrf_k": 60}}
    result = deep_merge(base, {})
    assert result == base


def test_deep_merge_empty_base_returns_overlay() -> None:
    """Empty base + non-empty overlay returns overlay."""
    base: dict[str, Any] = {}
    overlay = {"provider": "ollama"}
    result = deep_merge(base, overlay)
    assert result == overlay


def test_deep_merge_does_not_mutate_inputs() -> None:
    """Both inputs are left unchanged so callers can safely reuse them."""
    base = {"provider": "azure_foundry", "retrieval": {"rrf_k": 60}}
    overlay = {"retrieval": {"rrf_k": 10}}
    base_snapshot = {"provider": "azure_foundry", "retrieval": {"rrf_k": 60}}
    overlay_snapshot = {"retrieval": {"rrf_k": 10}}

    deep_merge(base, overlay)

    assert base == base_snapshot, "base must not be mutated"
    assert overlay == overlay_snapshot, "overlay must not be mutated"


def test_deep_merge_overlay_dict_replaces_base_scalar() -> None:
    """Type mismatch — overlay's dict structure wins over base's scalar.

    Operators don't usually do this, but the loader must not crash.
    """
    base = {"retrieval": "bm25_primary"}
    overlay = {"retrieval": {"fusion_strategy": "rrf", "rrf_k": 10}}

    result = deep_merge(base, overlay)

    assert result["retrieval"] == {"fusion_strategy": "rrf", "rrf_k": 10}


# ---------------------------------------------------------------------------
# resolve_layered_paths — env-driven resolution matrix
# ---------------------------------------------------------------------------


def _write(path: Path, body: str = "") -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_resolve_layered_paths_overlay_env_returns_base_and_overlay(tmp_path: Path) -> None:
    """When KAIRIX_CONFIG_OVERLAY_PATH is set, return (base, overlay).

    Tests drive the resolver via the ``env`` kwarg (F2-clean) — no
    process-env mutation.
    """
    base = _write(tmp_path / "base.yaml", "_schema_version: 1\nprovider: azure_foundry\n")
    overlay = _write(tmp_path / "overlay.yaml", "provider: ollama\n")
    env = {
        "KAIRIX_CONFIG_OVERLAY_PATH": str(overlay),
        "KAIRIX_CONFIG_BASE_PATH": str(base),
    }

    resolved_base, resolved_overlay = resolve_layered_paths(env=env)

    assert resolved_base == base
    assert resolved_overlay == overlay


def test_resolve_layered_paths_overlay_set_base_unset_uses_default_base(tmp_path: Path) -> None:
    """When KAIRIX_CONFIG_OVERLAY_PATH is set but KAIRIX_CONFIG_BASE_PATH is not,
    the resolver falls back to the image's installed base path.

    Production callers will land on ``/opt/kairix/kairix.config.yaml``;
    the test passes a default explicitly to confirm the fall-through wires
    correctly without depending on whether that file actually exists at
    test time.
    """
    overlay = _write(tmp_path / "overlay.yaml", "provider: ollama\n")
    image_base = _write(tmp_path / "image-base.yaml", "_schema_version: 1\nprovider: azure_foundry\n")
    env = {"KAIRIX_CONFIG_OVERLAY_PATH": str(overlay)}

    resolved_base, resolved_overlay = resolve_layered_paths(env=env, image_base_default=image_base)

    assert resolved_base == image_base
    assert resolved_overlay == overlay


def test_resolve_layered_paths_legacy_single_file_path(tmp_path: Path) -> None:
    """Legacy KAIRIX_CONFIG_PATH without overlay → single-file mode (overlay=None)."""
    single = _write(tmp_path / "single.yaml", "provider: azure_foundry\n")
    env = {"KAIRIX_CONFIG_PATH": str(single)}

    resolved_base, resolved_overlay = resolve_layered_paths(env=env)

    assert resolved_base == single
    assert resolved_overlay is None


def test_resolve_layered_paths_no_env_returns_none_pair() -> None:
    """No env vars + no cwd file → (None, None) → defaults at parse time."""
    resolved_base, resolved_overlay = resolve_layered_paths(env={}, image_base_default=Path("/nonexistent"))
    assert resolved_base is None
    assert resolved_overlay is None


def test_resolve_layered_paths_overlay_wins_over_legacy(tmp_path: Path) -> None:
    """When both KAIRIX_CONFIG_OVERLAY_PATH and KAIRIX_CONFIG_PATH are set,
    the layered mode wins — legacy is the deprecated path.

    Mixing them is operator error; the resolver picks the modern one and
    callers can log a deprecation note. The resolver itself is pure: it
    returns the layered pair.
    """
    base = _write(tmp_path / "base.yaml", "_schema_version: 1\nprovider: azure_foundry\n")
    overlay = _write(tmp_path / "overlay.yaml", "provider: ollama\n")
    legacy = _write(tmp_path / "legacy.yaml", "provider: bedrock\n")
    env = {
        "KAIRIX_CONFIG_OVERLAY_PATH": str(overlay),
        "KAIRIX_CONFIG_BASE_PATH": str(base),
        "KAIRIX_CONFIG_PATH": str(legacy),
    }

    resolved_base, resolved_overlay = resolve_layered_paths(env=env)

    # Layered mode is picked; legacy is ignored.
    assert resolved_base == base
    assert resolved_overlay == overlay


# ---------------------------------------------------------------------------
# validate_schema_compat — version-mismatch refusal
# ---------------------------------------------------------------------------


def test_validate_schema_compat_overlay_min_equals_base_ok() -> None:
    """Equal versions pass."""
    base = {"_schema_version": 1, "provider": "azure_foundry"}
    overlay = {"_schema_version_required_min": 1, "provider": "ollama"}
    # Should not raise.
    validate_schema_compat(base, overlay)


def test_validate_schema_compat_overlay_min_below_base_ok() -> None:
    """Overlay declares it works against schema ≥1 and base is v2 → ok."""
    base = {"_schema_version": 2}
    overlay = {"_schema_version_required_min": 1}
    validate_schema_compat(base, overlay)


def test_validate_schema_compat_overlay_min_above_base_raises() -> None:
    """Overlay declares it needs schema ≥2 but base is v1 → refuse.

    Error message must be actionable: state the gap, point at the
    upgrade-runbook, and suggest dropping `_schema_version_required_min`
    if the operator has confirmed compatibility manually.
    """
    base = {"_schema_version": 1}
    overlay = {"_schema_version_required_min": 2}

    with pytest.raises(ConfigValidationError) as info:
        validate_schema_compat(base, overlay)

    msg = str(info.value)
    assert "_schema_version" in msg
    assert "1" in msg and "2" in msg
    assert "fix:" in msg or "run:" in msg or "next:" in msg, (
        "F21 action marker required in operator-facing error message"
    )


def test_validate_schema_compat_no_overlay_version_passes() -> None:
    """Overlay without _schema_version_required_min always passes — operator
    accepts whatever schema the base ships."""
    base = {"_schema_version": 5}
    overlay = {"provider": "ollama"}
    validate_schema_compat(base, overlay)


def test_validate_schema_compat_no_base_version_warns_treats_as_zero() -> None:
    """Base without _schema_version is treated as version 0 (legacy).
    A non-zero overlay-required-min raises against the implicit zero.
    """
    base = {"provider": "azure_foundry"}  # no _schema_version
    overlay = {"_schema_version_required_min": 1}

    with pytest.raises(ConfigValidationError) as info:
        validate_schema_compat(base, overlay)

    assert "_schema_version" in str(info.value)


def test_validate_schema_compat_overlay_none_passes() -> None:
    """If there's no overlay at all, no schema-compat check is meaningful."""
    base = {"_schema_version": 1}
    validate_schema_compat(base, None)
