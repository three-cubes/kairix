"""Contract probes for kairix.core.search.config_validator.validate_config.

One probe per documented validation rule from the module docstring + code.
Boundary cases for empty, malformed, and edge inputs.

All tests drive through the public surface: validate_config(data: dict) -> list[str].
No private-fn imports. No monkeypatching.
"""

from __future__ import annotations

import pytest

from kairix.core.search.config_validator import validate_config

pytestmark = pytest.mark.contract


# -- empty / minimal -----------------------------------------------------------


def test_empty_dict_returns_empty_error_list() -> None:
    """Absence of every section is valid (search-everything fallback)."""
    assert validate_config({}) == []


def test_collections_none_is_valid() -> None:
    """Explicit None for collections is the documented fallback."""
    assert validate_config({"collections": None}) == []


def test_agents_none_is_valid() -> None:
    """Explicit None for agents is valid (no all-agents support)."""
    assert validate_config({"agents": None}) == []


# -- collections type / shape errors ------------------------------------------


def test_collections_as_list_is_rejected_with_mapping_error() -> None:
    errors = validate_config({"collections": ["docs", "research"]})
    assert errors == ["collections: must be a mapping"]


def test_collections_as_string_is_rejected_with_mapping_error() -> None:
    errors = validate_config({"collections": "docs"})
    assert errors == ["collections: must be a mapping"]


def test_collections_shared_as_dict_reports_must_be_list() -> None:
    errors = validate_config({"collections": {"shared": {"name": "x"}}})
    assert errors == ["collections.shared: must be a list"]


def test_collections_shared_as_string_reports_must_be_list() -> None:
    errors = validate_config({"collections": {"shared": "docs"}})
    assert errors == ["collections.shared: must be a list"]


def test_collections_shared_empty_list_is_valid() -> None:
    """Boundary: empty list of shared collections is structurally fine."""
    assert validate_config({"collections": {"shared": []}}) == []


# -- collection item rules -----------------------------------------------------


def test_collection_item_as_string_reports_mapping_error() -> None:
    errors = validate_config({"collections": {"shared": ["docs"]}})
    assert any("collections.shared[0]: must be a mapping with name + path" in e for e in errors)


def test_collection_missing_name_reports_index_and_field() -> None:
    errors = validate_config({"collections": {"shared": [{"path": "docs"}]}})
    assert any("collections.shared[0]: missing required 'name'" in e for e in errors)


def test_collection_empty_string_name_reports_missing_name() -> None:
    """Boundary: '' is falsy -> treated as missing."""
    errors = validate_config({"collections": {"shared": [{"name": "", "path": "x"}]}})
    assert any("missing required 'name'" in e for e in errors)


def test_collection_missing_path_reports_named_field() -> None:
    errors = validate_config({"collections": {"shared": [{"name": "docs"}]}})
    assert any("collections.shared[0] (docs): missing required 'path'" in e for e in errors)


def test_collection_path_empty_string_is_accepted_as_present() -> None:
    """Edge: 'path' present (even empty) satisfies the 'in item' check."""
    # Documented rule: missing path is the error condition, not empty.
    errors = validate_config({"collections": {"shared": [{"name": "docs", "path": ""}]}})
    assert not any("missing required 'path'" in e for e in errors)


def test_duplicate_collection_names_reported_with_name() -> None:
    errors = validate_config(
        {"collections": {"shared": [{"name": "docs", "path": "a"}, {"name": "docs", "path": "b"}]}}
    )
    assert any("duplicate collection name 'docs'" in e for e in errors)


# -- retrieval overrides -------------------------------------------------------


def test_retrieval_as_list_is_rejected() -> None:
    errors = validate_config({"collections": {"shared": [{"name": "docs", "path": "x", "retrieval": ["rrf_k"]}]}})
    assert any("'retrieval' must be a mapping" in e for e in errors)


def test_unknown_retrieval_override_is_named_explicitly() -> None:
    errors = validate_config({"collections": {"shared": [{"name": "docs", "path": "x", "retrieval": {"flubber": 1}}]}})
    found = [e for e in errors if "unknown retrieval override key" in e]
    assert found, f"expected unknown-key error, got: {errors}"
    assert "'flubber'" in found[0] or "flubber" in found[0]


@pytest.mark.parametrize(
    "key",
    [
        "fusion_strategy",
        "rrf_k",
        "bm25_limit",
        "vec_limit",
        "skip_vector",
        "entity",
        "procedural",
        "temporal",
        "rerank",
        "rerank_intents",
    ],
)
def test_each_documented_override_key_passes(key: str) -> None:
    """Every name listed in the module-level frozenset is accepted."""
    errors = validate_config({"collections": {"shared": [{"name": "docs", "path": "x", "retrieval": {key: 1}}]}})
    assert errors == [], f"expected {key} accepted, got {errors}"


def test_retrieval_empty_dict_is_accepted() -> None:
    errors = validate_config({"collections": {"shared": [{"name": "docs", "path": "x", "retrieval": {}}]}})
    assert errors == []


# -- agent_pattern -------------------------------------------------------------


def test_agent_pattern_non_string_is_rejected() -> None:
    errors = validate_config({"collections": {"shared": [], "agent_pattern": 7}})
    assert any("collections.agent_pattern: must be a string template" in e for e in errors)


def test_agent_pattern_missing_placeholder_is_rejected() -> None:
    errors = validate_config({"collections": {"shared": [], "agent_pattern": "memory-only"}})
    assert any("must contain '{agent}' placeholder" in e for e in errors)


def test_agent_pattern_with_placeholder_passes() -> None:
    errors = validate_config({"collections": {"shared": [], "agent_pattern": "{agent}-mem"}})
    assert errors == []


# -- agents type / shape -------------------------------------------------------


def test_agents_as_dict_reports_must_be_list() -> None:
    errors = validate_config({"agents": {"name": "alpha"}})
    assert errors == ["agents: must be a list"]


def test_agents_as_string_reports_must_be_list() -> None:
    errors = validate_config({"agents": "alpha"})
    assert errors == ["agents: must be a list"]


def test_agents_empty_list_is_valid() -> None:
    """Boundary: empty list is fine."""
    assert validate_config({"agents": []}) == []


def test_agent_item_as_string_reports_mapping_error() -> None:
    errors = validate_config({"agents": ["alpha"]})
    assert any("agents[0]: must be a mapping" in e for e in errors)


# -- agent rules ---------------------------------------------------------------


def test_agent_missing_name_reports_with_index() -> None:
    errors = validate_config({"agents": [{"collection": "x"}]})
    assert any("agents[0]: missing required 'name'" in e for e in errors)


def test_agent_empty_name_reports_missing_name() -> None:
    """Boundary: '' is falsy -> treated as missing."""
    errors = validate_config({"agents": [{"name": ""}]})
    assert any("missing required 'name'" in e for e in errors)


def test_duplicate_agent_name_is_reported() -> None:
    errors = validate_config({"agents": [{"name": "alpha"}, {"name": "alpha"}]})
    assert any("duplicate agent name 'alpha'" in e for e in errors)


def test_agent_collection_non_string_reports_error() -> None:
    """If the operator passes 'collection: [list]' the validator catches it."""
    errors = validate_config({"agents": [{"name": "alpha", "collection": ["a", "b"]}]})
    assert any("agents[0] (alpha): collection must be a string" in e for e in errors)


def test_agent_write_path_as_int_reports_error() -> None:
    errors = validate_config({"agents": [{"name": "alpha", "write_path": 42}]})
    assert any("write_path must be a string" in e for e in errors)


# -- write_path overlap --------------------------------------------------------


def test_identical_write_paths_report_duplicate() -> None:
    errors = validate_config(
        {
            "agents": [
                {"name": "alpha", "write_path": "agents/shared"},
                {"name": "beta", "write_path": "agents/shared"},
            ]
        }
    )
    assert any("duplicates agent 'alpha'" in e for e in errors)


def test_prefix_overlap_is_reported() -> None:
    errors = validate_config(
        {
            "agents": [
                {"name": "alpha", "write_path": "agents/alpha"},
                {"name": "beta", "write_path": "agents/alpha/sub"},
            ]
        }
    )
    overlap = [e for e in errors if "overlaps" in e]
    assert overlap, f"expected overlap error, got {errors}"
    assert "'alpha'" in overlap[0]


def test_reverse_prefix_overlap_is_reported() -> None:
    """beta declared first; alpha extends beta's path."""
    errors = validate_config(
        {
            "agents": [
                {"name": "beta", "write_path": "agents/beta/notes"},
                {"name": "alpha", "write_path": "agents/beta"},
            ]
        }
    )
    overlap = [e for e in errors if "overlaps" in e]
    assert overlap, f"expected overlap error, got {errors}"


def test_disjoint_write_paths_pass() -> None:
    errors = validate_config(
        {
            "agents": [
                {"name": "alpha", "write_path": "agents/alpha"},
                {"name": "beta", "write_path": "agents/beta"},
            ]
        }
    )
    assert errors == []


def test_sibling_paths_with_shared_prefix_substring_dont_overlap() -> None:
    """'agents/alpha' should not overlap 'agents/alpha-extra' — only path-segment overlap counts."""
    errors = validate_config(
        {
            "agents": [
                {"name": "alpha", "write_path": "agents/alpha"},
                {"name": "alpha2", "write_path": "agents/alpha-extra"},
            ]
        }
    )
    assert errors == [], f"sibling distinct dirs should not overlap, got {errors}"


def test_empty_write_path_is_skipped_in_overlap_check() -> None:
    """Documented: empty write_path means read-only; not entered into overlap set."""
    errors = validate_config(
        {
            "agents": [
                {"name": "alpha", "write_path": ""},
                {"name": "beta", "write_path": ""},
            ]
        }
    )
    assert errors == []


def test_trailing_slash_overlap_is_normalised() -> None:
    """'agents/alpha/' vs 'agents/alpha/sub' should still be detected as overlap."""
    errors = validate_config(
        {
            "agents": [
                {"name": "alpha", "write_path": "agents/alpha/"},
                {"name": "beta", "write_path": "agents/alpha/sub"},
            ]
        }
    )
    assert any("overlaps" in e for e in errors)


# -- combined behaviours -------------------------------------------------------


def test_full_valid_config_returns_no_errors() -> None:
    data = {
        "collections": {
            "shared": [
                {"name": "docs", "path": "docs", "retrieval": {"rrf_k": 30, "bm25_limit": 12}},
                {"name": "research", "path": "research"},
            ],
            "agent_pattern": "{agent}-memory",
        },
        "agents": [
            {"name": "alpha", "write_path": "agents/alpha"},
            {"name": "beta", "write_path": "agents/beta"},
            {"name": "gamma", "read_only": True},
        ],
    }
    assert validate_config(data) == []


def test_multiple_independent_errors_all_reported() -> None:
    """Validator collects errors instead of raising on first."""
    data = {
        "collections": {"shared": [{"path": "x"}]},  # missing name
        "agents": [{"name": ""}],  # missing name
    }
    errors = validate_config(data)
    assert len(errors) >= 2
    assert any("collections.shared[0]" in e for e in errors)
    assert any("agents[0]" in e for e in errors)


def test_validate_never_raises_on_arbitrary_garbage() -> None:
    """Documented: validator returns errors as strings, never raises."""
    # Mixed garbage: a string for collections, a dict for agents
    out = validate_config({"collections": "garbage", "agents": {"k": "v"}})
    assert isinstance(out, list)
    assert all(isinstance(e, str) for e in out)
    assert len(out) >= 2
