"""pytest-bdd binding for config_layering.feature."""

from pathlib import Path

import pytest
from pytest_bdd import scenario

FEATURE = str(Path(__file__).parent / "features" / "config_layering.feature")

pytestmark = pytest.mark.bdd


@scenario(FEATURE, "Base alone resolves every required key when no overlay is set")
def test_base_alone_resolves_required_keys() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "Overlay overrides specific keys without dropping required ones")
def test_overlay_overrides_without_dropping_required() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "Overlay can override the provider plugin choice")
def test_overlay_overrides_provider() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "Overlay collections list REPLACES base list (not concat)")
def test_overlay_collections_replace_base() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "Nested dict merge — overlay's retrieval.boosts.entity merges with base's retrieval")
def test_nested_dict_merge() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "Overlay requires a newer schema version than the base ships → startup refuses")
def test_schema_version_mismatch_refuses() -> None:
    """Body populated by @scenario from the .feature file."""


@scenario(FEATURE, "Legacy single-file KAIRIX_CONFIG_PATH still works (no overlay declared)")
def test_legacy_single_file_still_works() -> None:
    """Body populated by @scenario from the .feature file."""
