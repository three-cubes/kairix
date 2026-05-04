"""Unit tests for validate_config — kairix.config.yaml schema validator."""

from __future__ import annotations

import pytest

from kairix.core.search.config_validator import validate_config


@pytest.mark.unit
def test_empty_dict_is_valid() -> None:
    """Absence of every section is valid (= search-everything fallback)."""
    assert validate_config({}) == []


@pytest.mark.unit
def test_well_formed_collections_pass() -> None:
    data = {
        "collections": {
            "shared": [{"name": "docs", "path": "docs"}, {"name": "research", "path": "research"}],
            "agent_pattern": "{agent}-memory",
        }
    }
    assert validate_config(data) == []


@pytest.mark.unit
def test_collection_missing_name_reports_error() -> None:
    data = {"collections": {"shared": [{"path": "docs"}]}}
    errors = validate_config(data)
    assert any("missing required 'name'" in e for e in errors)


@pytest.mark.unit
def test_collection_missing_path_reports_error() -> None:
    data = {"collections": {"shared": [{"name": "docs"}]}}
    errors = validate_config(data)
    assert any("missing required 'path'" in e for e in errors)


@pytest.mark.unit
def test_duplicate_collection_name_reports_error() -> None:
    data = {"collections": {"shared": [{"name": "docs", "path": "a"}, {"name": "docs", "path": "b"}]}}
    errors = validate_config(data)
    assert any("duplicate collection" in e for e in errors)


@pytest.mark.unit
def test_unknown_retrieval_override_key_reports_error() -> None:
    data = {
        "collections": {
            "shared": [{"name": "docs", "path": "docs", "retrieval": {"not_a_real_key": True}}]
        }
    }
    errors = validate_config(data)
    assert any("unknown retrieval override key" in e for e in errors)


@pytest.mark.unit
def test_known_retrieval_override_keys_pass() -> None:
    data = {
        "collections": {
            "shared": [{"name": "docs", "path": "docs", "retrieval": {"rrf_k": 30, "bm25_limit": 10}}]
        }
    }
    assert validate_config(data) == []


@pytest.mark.unit
def test_agent_pattern_without_placeholder_reports_error() -> None:
    data = {"collections": {"shared": [], "agent_pattern": "memory-only"}}
    errors = validate_config(data)
    assert any("must contain '{agent}'" in e for e in errors)


@pytest.mark.unit
def test_agent_missing_name_reports_error() -> None:
    data = {"agents": [{"collection": "alpha-memory"}]}
    errors = validate_config(data)
    assert any("missing required 'name'" in e for e in errors)


@pytest.mark.unit
def test_duplicate_agent_name_reports_error() -> None:
    data = {"agents": [{"name": "alpha"}, {"name": "alpha"}]}
    errors = validate_config(data)
    assert any("duplicate agent" in e for e in errors)


@pytest.mark.unit
def test_overlapping_agent_write_paths_report_error() -> None:
    """One agent's write_path being a prefix of another's is the dangerous case."""
    data = {
        "agents": [
            {"name": "alpha", "write_path": "agents/alpha"},
            {"name": "beta", "write_path": "agents/alpha/sub"},
        ]
    }
    errors = validate_config(data)
    assert any("overlaps" in e for e in errors)


@pytest.mark.unit
def test_identical_write_paths_report_error() -> None:
    data = {
        "agents": [
            {"name": "alpha", "write_path": "agents/shared"},
            {"name": "beta", "write_path": "agents/shared"},
        ]
    }
    errors = validate_config(data)
    assert any("duplicates" in e for e in errors)


@pytest.mark.unit
def test_well_formed_agents_pass() -> None:
    data = {
        "agents": [
            {"name": "alpha", "write_path": "agents/alpha"},
            {"name": "beta", "write_path": "agents/beta"},
            {"name": "gamma", "read_only": True},
        ]
    }
    assert validate_config(data) == []


@pytest.mark.unit
def test_collections_must_be_mapping() -> None:
    errors = validate_config({"collections": ["not", "a", "mapping"]})
    assert any("must be a mapping" in e for e in errors)


@pytest.mark.unit
def test_agents_must_be_list() -> None:
    errors = validate_config({"agents": {"name": "alpha"}})
    assert any("must be a list" in e for e in errors)
