"""Unit tests for validate_config — kairix.config.yaml schema validator."""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from kairix.core.search.config_validator import main as validator_main
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
    data = {"collections": {"shared": [{"name": "docs", "path": "docs", "retrieval": {"not_a_real_key": True}}]}}
    errors = validate_config(data)
    assert any("unknown retrieval override key" in e for e in errors)


@pytest.mark.unit
def test_known_retrieval_override_keys_pass() -> None:
    data = {"collections": {"shared": [{"name": "docs", "path": "docs", "retrieval": {"rrf_k": 30, "bm25_limit": 10}}]}}
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


# ---------------------------------------------------------------------------
# main() CLI — exercise the operator entry point
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_main_no_subcommand_prints_help_returns_1() -> None:
    """When no subcommand is passed, main prints help and returns 1."""
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        rc = validator_main([])
    assert rc == 1
    # argparse prints help to stdout
    assert "validate" in stdout.getvalue() or "usage" in stdout.getvalue().lower()


@pytest.mark.unit
def test_main_with_explicit_valid_config(tmp_path: Path) -> None:
    """When the YAML is well-formed and valid, main prints OK and returns 0."""
    cfg = tmp_path / "kairix.config.yaml"
    cfg.write_text(
        """\
collections:
  shared:
    - name: docs
      path: docs
  agent_pattern: "{agent}-memory"
""",
        encoding="utf-8",
    )
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        rc = validator_main(["validate", str(cfg)])
    assert rc == 0
    assert "OK" in stdout.getvalue()


@pytest.mark.unit
def test_main_with_invalid_config(tmp_path: Path) -> None:
    """When the YAML parses but fails schema rules, main lists errors and returns 1."""
    cfg = tmp_path / "kairix.config.yaml"
    cfg.write_text(
        """\
collections:
  shared:
    - path: docs   # missing required 'name'
""",
        encoding="utf-8",
    )
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        rc = validator_main(["validate", str(cfg)])
    assert rc == 1
    assert "validation error" in stdout.getvalue().lower()


@pytest.mark.unit
def test_main_with_missing_file(tmp_path: Path) -> None:
    """A non-existent path prints an error and returns 1."""
    missing = tmp_path / "missing.yaml"
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        rc = validator_main(["validate", str(missing)])
    assert rc == 1
    assert "not found" in stdout.getvalue().lower()


@pytest.mark.unit
def test_main_with_invalid_yaml(tmp_path: Path) -> None:
    """When YAML parsing fails, main prints the error and returns 1."""
    cfg = tmp_path / "kairix.config.yaml"
    cfg.write_text("collections: [unclosed bracket", encoding="utf-8")
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        rc = validator_main(["validate", str(cfg)])
    assert rc == 1
    assert "yaml" in stdout.getvalue().lower() or "parse" in stdout.getvalue().lower()


@pytest.mark.unit
def test_main_without_path_resolves_via_cwd(tmp_path: Path, monkeypatch) -> None:
    """When no path arg, main calls _resolve_config_path which falls back to
    looking for ``kairix.config.yaml`` in cwd. Empty cwd → 'No config file'."""
    monkeypatch.chdir(tmp_path)
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        rc = validator_main(["validate"])
    assert rc == 1
    assert "No config file" in stdout.getvalue() or "not found" in stdout.getvalue().lower()


@pytest.mark.unit
def test_main_without_path_finds_cwd_config(tmp_path: Path, monkeypatch) -> None:
    """When no path arg AND kairix.config.yaml in cwd, the cwd file is validated."""
    cfg = tmp_path / "kairix.config.yaml"
    cfg.write_text("collections:\n  shared: []\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        rc = validator_main(["validate"])
    assert rc == 0
    assert "OK" in stdout.getvalue()


@pytest.mark.unit
def test_main_yaml_loads_to_empty_dict_is_valid(tmp_path: Path) -> None:
    """Empty YAML file parses to None → validate_config gets {} → no errors."""
    cfg = tmp_path / "kairix.config.yaml"
    cfg.write_text("", encoding="utf-8")
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        rc = validator_main(["validate", str(cfg)])
    assert rc == 0
