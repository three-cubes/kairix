"""Step definitions for tests/bdd/features/config_layering.feature.

Drives ``kairix.core.search.config_loader.load_config`` end-to-end with
real YAML files in ``tmp_path``. The ``env`` dict is built explicitly
per scenario (F2-clean — no ``monkeypatch.setenv``) and flows into the
loader's public seam.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

from kairix.core.search.config_loader import (
    ConfigValidationError,
    load_config,
    load_layered_yaml,
)

pytestmark = pytest.mark.bdd


_BASE_FULL = """\
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


@pytest.fixture
def _cfg_state(tmp_path: Path) -> dict[str, Any]:
    """Per-scenario state: paths, env-dict, captured config / error."""
    state: dict[str, Any] = {
        "tmp_path": tmp_path,
        "env": {},
        "base_path": None,
        "overlay_path": None,
        "cfg": None,
        "merged_yaml": None,
        "error": None,
    }
    return state


# ---------------------------------------------------------------------------
# Background
# ---------------------------------------------------------------------------


@given("an image-bundled base config that ships every required key")
def _background_base(_cfg_state: dict[str, Any]) -> None:
    p = _cfg_state["tmp_path"] / "base.yaml"
    p.write_text(_BASE_FULL, encoding="utf-8")
    _cfg_state["base_path"] = p
    _cfg_state["env"]["KAIRIX_CONFIG_BASE_PATH"] = str(p)


@given("the base config declares a schema version")
def _background_schema(_cfg_state: dict[str, Any]) -> None:
    # _BASE_FULL already declares _schema_version: 1 — no extra action.
    pass


# ---------------------------------------------------------------------------
# Givens
# ---------------------------------------------------------------------------


@given("no operator overlay file is configured")
def _no_overlay(_cfg_state: dict[str, Any]) -> None:
    # env starts empty for overlay; explicit assertion of intent.
    assert "KAIRIX_CONFIG_OVERLAY_PATH" not in _cfg_state["env"]


@given(parsers.parse("an operator overlay that sets `retrieval.fusion_strategy: rrf`"))
def _overlay_fusion_rrf(_cfg_state: dict[str, Any]) -> None:
    p = _cfg_state["tmp_path"] / "overlay.yaml"
    p.write_text("retrieval:\n  fusion_strategy: rrf\n", encoding="utf-8")
    _cfg_state["overlay_path"] = p
    _cfg_state["env"]["KAIRIX_CONFIG_OVERLAY_PATH"] = str(p)


@given("the overlay does NOT declare `provider:`")
def _overlay_no_provider(_cfg_state: dict[str, Any]) -> None:
    text = _cfg_state["overlay_path"].read_text(encoding="utf-8")
    assert "provider:" not in text, "overlay must not declare provider for this scenario"


@given(parsers.parse("an operator overlay that sets `provider: {value}`"))
def _overlay_provider(_cfg_state: dict[str, Any], value: str) -> None:
    p = _cfg_state["tmp_path"] / "overlay.yaml"
    p.write_text(f"provider: {value}\n", encoding="utf-8")
    _cfg_state["overlay_path"] = p
    _cfg_state["env"]["KAIRIX_CONFIG_OVERLAY_PATH"] = str(p)


@given(parsers.parse("an operator overlay that sets `collections.shared` to a 2-item list"))
def _overlay_collections_2(_cfg_state: dict[str, Any]) -> None:
    p = _cfg_state["tmp_path"] / "overlay.yaml"
    p.write_text(
        "collections:\n  shared:\n    - name: alpha\n      path: a\n    - name: beta\n      path: b\n",
        encoding="utf-8",
    )
    _cfg_state["overlay_path"] = p
    _cfg_state["env"]["KAIRIX_CONFIG_OVERLAY_PATH"] = str(p)


@given(parsers.parse("the base config ships a 7-item `collections.shared` list"))
def _base_collections_7(_cfg_state: dict[str, Any]) -> None:
    # Re-write base with a 7-item collections list, schema_version + provider intact.
    items = "\n".join(f"    - name: c{i}\n      path: p{i}" for i in range(7))
    body = f"_schema_version: 1\nprovider: azure_foundry\ncollections:\n  shared:\n{items}\n"
    p = _cfg_state["base_path"]
    p.write_text(body, encoding="utf-8")


@given(
    parsers.parse(
        "a base config with `retrieval.fusion_strategy: bm25_primary` and `retrieval.boosts.entity.factor: 0.20`"
    )
)
def _base_with_nested(_cfg_state: dict[str, Any]) -> None:
    # Already set in _BASE_FULL.
    pass


@given(parsers.parse("an overlay that sets only `retrieval.boosts.entity.factor: 0.50`"))
def _overlay_nested_factor(_cfg_state: dict[str, Any]) -> None:
    p = _cfg_state["tmp_path"] / "overlay.yaml"
    p.write_text(
        "retrieval:\n  boosts:\n    entity:\n      factor: 0.50\n",
        encoding="utf-8",
    )
    _cfg_state["overlay_path"] = p
    _cfg_state["env"]["KAIRIX_CONFIG_OVERLAY_PATH"] = str(p)


@given(parsers.parse("an image-bundled base config with `_schema_version: 1`"))
def _base_schema_v1(_cfg_state: dict[str, Any]) -> None:
    p = _cfg_state["tmp_path"] / "base.yaml"
    p.write_text("_schema_version: 1\nprovider: azure_foundry\n", encoding="utf-8")
    _cfg_state["base_path"] = p
    _cfg_state["env"]["KAIRIX_CONFIG_BASE_PATH"] = str(p)


@given(parsers.parse("an operator overlay that declares `_schema_version_required_min: 2`"))
def _overlay_min_v2(_cfg_state: dict[str, Any]) -> None:
    p = _cfg_state["tmp_path"] / "overlay.yaml"
    p.write_text("_schema_version_required_min: 2\nprovider: ollama\n", encoding="utf-8")
    _cfg_state["overlay_path"] = p
    _cfg_state["env"]["KAIRIX_CONFIG_OVERLAY_PATH"] = str(p)


@given("the operator has only `KAIRIX_CONFIG_PATH` set to a complete single file")
def _legacy_single(_cfg_state: dict[str, Any]) -> None:
    p = _cfg_state["tmp_path"] / "legacy.yaml"
    p.write_text("provider: bedrock\nretrieval:\n  fusion_strategy: rrf\n  rrf_k: 30\n", encoding="utf-8")
    # Clear the layered-mode env so the legacy branch resolves.
    _cfg_state["env"].pop("KAIRIX_CONFIG_BASE_PATH", None)
    _cfg_state["env"].pop("KAIRIX_CONFIG_OVERLAY_PATH", None)
    _cfg_state["env"]["KAIRIX_CONFIG_PATH"] = str(p)
    _cfg_state["legacy_path"] = p


@given("no `KAIRIX_CONFIG_OVERLAY_PATH` is set")
def _no_overlay_env(_cfg_state: dict[str, Any]) -> None:
    assert "KAIRIX_CONFIG_OVERLAY_PATH" not in _cfg_state["env"]


# ---------------------------------------------------------------------------
# Whens
# ---------------------------------------------------------------------------


@when("kairix loads its configuration")
def _when_load(_cfg_state: dict[str, Any]) -> None:
    try:
        _cfg_state["cfg"] = load_config(env=_cfg_state["env"])
        _cfg_state["merged_yaml"] = load_layered_yaml(env=_cfg_state["env"])
    except ConfigValidationError as exc:
        _cfg_state["error"] = exc


# ---------------------------------------------------------------------------
# Thens
# ---------------------------------------------------------------------------


@then("the resolved config has the base's `provider:` value")
def _then_provider_from_base(_cfg_state: dict[str, Any]) -> None:
    assert _cfg_state["error"] is None
    assert _cfg_state["cfg"].provider == "azure_foundry"


@then("the resolved config has the base's retrieval defaults")
def _then_retrieval_defaults(_cfg_state: dict[str, Any]) -> None:
    cfg = _cfg_state["cfg"]
    assert cfg.fusion_strategy == "bm25_primary"
    assert cfg.entity.factor == 0.20


@then("no schema-version mismatch error is raised")
def _then_no_schema_error(_cfg_state: dict[str, Any]) -> None:
    assert _cfg_state["error"] is None


@then(parsers.parse('the resolved config has `retrieval.fusion_strategy == "{value}"` (from overlay)'))
def _then_fusion_from_overlay(_cfg_state: dict[str, Any], value: str) -> None:
    assert _cfg_state["cfg"].fusion_strategy == value


@then("the resolved config has the base's `provider:` value (inherited)")
def _then_provider_inherited(_cfg_state: dict[str, Any]) -> None:
    assert _cfg_state["cfg"].provider == "azure_foundry"


@then("the resolved config is internally consistent")
def _then_internally_consistent(_cfg_state: dict[str, Any]) -> None:
    # No exception; provider + fusion_strategy both populated.
    cfg = _cfg_state["cfg"]
    assert cfg.provider is not None
    assert cfg.fusion_strategy in ("bm25_primary", "rrf")


@then(parsers.parse('the resolved config has `provider == "{value}"` (overlay wins)'))
def _then_provider_overlay_wins(_cfg_state: dict[str, Any], value: str) -> None:
    assert _cfg_state["cfg"].provider == value


@then("the resolved config's `collections.shared` has exactly 2 items")
def _then_collections_two_items(_cfg_state: dict[str, Any]) -> None:
    shared = _cfg_state["merged_yaml"]["collections"]["shared"]
    assert len(shared) == 2


@then("the items are the overlay's two items in order")
def _then_collections_order(_cfg_state: dict[str, Any]) -> None:
    shared = _cfg_state["merged_yaml"]["collections"]["shared"]
    assert [s["name"] for s in shared] == ["alpha", "beta"]


@then(parsers.parse('the resolved config has `retrieval.fusion_strategy == "{value}"` (from base)'))
def _then_fusion_from_base(_cfg_state: dict[str, Any], value: str) -> None:
    assert _cfg_state["cfg"].fusion_strategy == value


@then(parsers.parse("the resolved config has `retrieval.boosts.entity.factor == {value:f}` (overlay wins)"))
def _then_entity_factor_overlay(_cfg_state: dict[str, Any], value: float) -> None:
    assert _cfg_state["cfg"].entity.factor == value


@then("a ConfigValidationError is raised")
def _then_config_error(_cfg_state: dict[str, Any]) -> None:
    assert isinstance(_cfg_state["error"], ConfigValidationError)


@then("the error message mentions the version mismatch")
def _then_message_version(_cfg_state: dict[str, Any]) -> None:
    msg = str(_cfg_state["error"])
    assert "_schema_version" in msg
    assert "1" in msg and "2" in msg


@then("the error message points the operator at the upgrade-runbook")
def _then_message_runbook(_cfg_state: dict[str, Any]) -> None:
    msg = str(_cfg_state["error"])
    assert "fix:" in msg or "next:" in msg or "run:" in msg


@then("the resolved config matches the single file")
def _then_legacy_matches(_cfg_state: dict[str, Any]) -> None:
    cfg = _cfg_state["cfg"]
    assert cfg.provider == "bedrock"
    assert cfg.fusion_strategy == "rrf"
    assert cfg.rrf_k == 30


@then("no deep-merge is performed")
def _then_no_merge(_cfg_state: dict[str, Any]) -> None:
    # The legacy file alone is the source; merged_yaml should match its content.
    merged = _cfg_state["merged_yaml"]
    assert merged.get("provider") == "bedrock"
