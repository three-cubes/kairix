"""Integration test: validator wired to real YAML loading + realistic operator configs.

Drives validate_config() from raw YAML on disk — the same path the CLI takes —
to demonstrate it catches operator mistakes that would otherwise crash production.

No monkeypatching, no private-fn imports — uses the public validate_config and
real YAML.safe_load.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
import yaml

from kairix.core.search.config_validator import validate_config

pytestmark = pytest.mark.integration


def _write_and_load(tmp_path: Path, name: str, body: str) -> dict:
    """Round-trip operator-supplied YAML through real PyYAML."""
    p = tmp_path / name
    p.write_text(dedent(body), encoding="utf-8")
    with p.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def test_realistic_well_formed_yaml_is_silent(tmp_path: Path) -> None:
    """Operator-supplied 3-collection, 4-agent config should pass cleanly."""
    data = _write_and_load(
        tmp_path,
        "kairix.config.yaml",
        """
        collections:
          shared:
            - name: docs
              path: docs
              retrieval:
                rrf_k: 32
                bm25_limit: 15
            - name: research
              path: research
              retrieval:
                fusion_strategy: rrf
                rerank: true
            - name: runbooks
              path: ops/runbooks
          agent_pattern: "{agent}-memory"

        agents:
          - name: alpha
            write_path: agents/alpha
          - name: beta
            write_path: agents/beta
          - name: gamma
            write_path: agents/gamma
          - name: reader
            read_only: true
        """,
    )
    assert validate_config(data) == []


def test_yaml_with_typo_in_override_key_is_caught(tmp_path: Path) -> None:
    """Common operator mistake: rrfk vs rrf_k. Without validator this silently fails at runtime."""
    data = _write_and_load(
        tmp_path,
        "kairix.config.yaml",
        """
        collections:
          shared:
            - name: docs
              path: docs
              retrieval:
                rrfk: 30           # typo: missing underscore
                bm25_limmit: 12    # typo: extra m
        """,
    )
    errors = validate_config(data)
    assert any("unknown retrieval override key" in e for e in errors)
    # Both typos should appear sorted in the same error message
    assert any("rrfk" in e and "bm25_limmit" in e for e in errors)


def test_yaml_with_overlapping_write_paths_is_caught(tmp_path: Path) -> None:
    """Two agents writing into nested paths is the dangerous case the validator
    must catch — would produce data corruption in production."""
    data = _write_and_load(
        tmp_path,
        "kairix.config.yaml",
        """
        agents:
          - name: alpha
            write_path: agents/shared
          - name: beta
            write_path: agents/shared/notes
        """,
    )
    errors = validate_config(data)
    assert any("overlaps" in e for e in errors)
    assert any("'alpha'" in e for e in errors)


def test_yaml_with_missing_required_fields_lists_each(tmp_path: Path) -> None:
    """An operator commits a config with two unrelated mistakes: validator
    must report both, not bail on first."""
    data = _write_and_load(
        tmp_path,
        "kairix.config.yaml",
        """
        collections:
          shared:
            - path: docs                # missing name
            - name: research            # missing path
          agent_pattern: "memory-{wrong}"  # missing {agent}
        agents:
          - collection: alpha-memory    # missing name
        """,
    )
    errors = validate_config(data)
    # Verify all four problems are reported (not just the first)
    assert any("missing required 'name'" in e and "collections.shared[0]" in e for e in errors)
    assert any("missing required 'path'" in e and "research" in e for e in errors)
    assert any("must contain '{agent}' placeholder" in e for e in errors)
    assert any("missing required 'name'" in e and "agents[0]" in e for e in errors)


def test_yaml_with_invalid_agent_pattern_type_is_caught(tmp_path: Path) -> None:
    """A YAML true/false/list slipped into agent_pattern is reported."""
    data = _write_and_load(
        tmp_path,
        "kairix.config.yaml",
        """
        collections:
          shared: []
          agent_pattern: 42
        """,
    )
    errors = validate_config(data)
    assert any("must be a string template" in e for e in errors)


def test_yaml_with_duplicate_collection_name_is_caught(tmp_path: Path) -> None:
    """Two collections with the same name would cause undefined retrieval-override behaviour."""
    data = _write_and_load(
        tmp_path,
        "kairix.config.yaml",
        """
        collections:
          shared:
            - name: docs
              path: a
            - name: docs
              path: b
        """,
    )
    errors = validate_config(data)
    assert any("duplicate collection name 'docs'" in e for e in errors)


def test_yaml_emits_only_errors_strings(tmp_path: Path) -> None:
    """Loop the validator over a corrupted operator config — never raises, always returns strings."""
    data = _write_and_load(
        tmp_path,
        "kairix.config.yaml",
        """
        collections:
          shared:
            - "not a mapping"
            - name: ok
              path: ok
              retrieval:
                bogus_key: 1
        agents:
          - 7
          - name: alpha
            write_path: 100
        """,
    )
    errors = validate_config(data)
    assert isinstance(errors, list)
    assert all(isinstance(e, str) for e in errors)
    # 4 distinct issues: not-a-mapping collection; bogus override; agent int;
    # write_path int.
    assert len(errors) >= 4


def test_empty_yaml_file_is_valid(tmp_path: Path) -> None:
    """A blank kairix.config.yaml (yaml.safe_load returns None) round-trips to {}."""
    p = tmp_path / "kairix.config.yaml"
    p.write_text("", encoding="utf-8")
    with p.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    assert validate_config(data) == []


def test_yaml_with_only_comments_is_valid(tmp_path: Path) -> None:
    data = _write_and_load(
        tmp_path,
        "kairix.config.yaml",
        """
        # operator notes
        # nothing configured yet
        """,
    )
    assert validate_config(data) == []
