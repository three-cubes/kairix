"""Tests for kairix YAML config loader."""

from __future__ import annotations

import textwrap

import pytest

from kairix.core.search.config import RetrievalConfig
from kairix.core.search.config_loader import (
    ConfigValidationError,
    _load_cached,
    _parse_config,
    _resolve_config_path,
    _validate_config,
    load_config,
    parse_collections,
)


@pytest.mark.unit
class TestParseConfig:
    @pytest.mark.unit
    def test_empty_dict_returns_defaults(self):
        cfg = _parse_config({})
        defaults = RetrievalConfig.defaults()
        assert cfg.entity.enabled == defaults.entity.enabled
        assert cfg.procedural.factor == defaults.procedural.factor

    @pytest.mark.unit
    def test_entity_enabled_false(self):
        cfg = _parse_config({"retrieval": {"boosts": {"entity": {"enabled": False}}}})
        assert cfg.entity.enabled is False

    @pytest.mark.unit
    def test_procedural_custom_factor(self):
        cfg = _parse_config({"retrieval": {"boosts": {"procedural": {"factor": 1.8}}}})
        assert cfg.procedural.factor == pytest.approx(1.8)

    @pytest.mark.unit
    def test_custom_path_patterns(self):
        cfg = _parse_config({"retrieval": {"boosts": {"procedural": {"path_patterns": [r"(?:^|/)docs/"]}}}})
        assert r"(?:^|/)docs/" in cfg.procedural.path_patterns

    @pytest.mark.unit
    def test_temporal_date_path_boost_enabled(self):
        cfg = _parse_config(
            {"retrieval": {"boosts": {"temporal": {"date_path_boost": {"enabled": True, "factor": 1.5}}}}}
        )
        assert cfg.temporal.date_path_boost_enabled is True
        assert cfg.temporal.date_path_boost_factor == pytest.approx(1.5)

    @pytest.mark.unit
    def test_temporal_chunk_date_boost_enabled(self):
        cfg = _parse_config(
            {
                "retrieval": {
                    "boosts": {
                        "temporal": {
                            "chunk_date_boost": {
                                "enabled": True,
                                "decay_halflife_days": 14,
                            }
                        }
                    }
                }
            }
        )
        assert cfg.temporal.chunk_date_boost_enabled is True
        assert cfg.temporal.chunk_date_decay_halflife_days == 14

    @pytest.mark.unit
    def test_temporal_chunk_date_guard_explicit_only_defaults_true(self):
        cfg = _parse_config({})
        assert cfg.temporal.chunk_date_boost_guard_explicit_only is True

    @pytest.mark.unit
    def test_temporal_chunk_date_guard_explicit_only_can_disable(self):
        cfg = _parse_config(
            {"retrieval": {"boosts": {"temporal": {"chunk_date_boost": {"guard_explicit_only": False}}}}}
        )
        assert cfg.temporal.chunk_date_boost_guard_explicit_only is False

    @pytest.mark.unit
    def test_rerank_config_parsed(self):
        cfg = _parse_config({"retrieval": {"rerank": {"enabled": True, "candidate_limit": 30}}})
        assert cfg.rerank.enabled is True
        assert cfg.rerank.candidate_limit == 30

    @pytest.mark.unit
    def test_rerank_defaults_disabled(self):
        cfg = _parse_config({})
        assert cfg.rerank.enabled is False


@pytest.mark.unit
class TestValidateConfig:
    @pytest.mark.unit
    def test_valid_defaults_pass(self):
        cfg = _parse_config({})
        _validate_config(cfg)  # should not raise
        assert True, "smoke: default config accepted without error"

    @pytest.mark.unit
    def test_entity_factor_out_of_range_raises(self):
        cfg = _parse_config({"retrieval": {"boosts": {"entity": {"factor": 99.0}}}})
        with pytest.raises(ConfigValidationError, match=r"entity\.factor"):
            _validate_config(cfg)

    @pytest.mark.unit
    def test_entity_cap_below_min_raises(self):
        cfg = _parse_config({"retrieval": {"boosts": {"entity": {"cap": 0.5}}}})
        with pytest.raises(ConfigValidationError, match=r"entity\.cap"):
            _validate_config(cfg)

    @pytest.mark.unit
    def test_procedural_factor_out_of_range_raises(self):
        cfg = _parse_config({"retrieval": {"boosts": {"procedural": {"factor": 0.5}}}})
        with pytest.raises(ConfigValidationError, match=r"procedural\.factor"):
            _validate_config(cfg)

    @pytest.mark.unit
    def test_multiple_errors_reported_together(self):
        cfg = _parse_config(
            {
                "retrieval": {
                    "boosts": {
                        "entity": {"factor": 99.0, "cap": 0.1},
                    }
                }
            }
        )
        with pytest.raises(ConfigValidationError) as exc_info:
            _validate_config(cfg)
        msg = str(exc_info.value)
        assert "entity.factor" in msg
        assert "entity.cap" in msg

    @pytest.mark.unit
    def test_invalid_config_not_silently_swallowed(self, tmp_path, monkeypatch):
        """ConfigValidationError must propagate — never fall back to defaults on invalid config."""
        pytest.importorskip("yaml")
        config_file = tmp_path / "kairix.config.yaml"
        config_file.write_text(
            textwrap.dedent("""
            retrieval:
              boosts:
                entity:
                  factor: 999.0
        """)
        )
        monkeypatch.setenv("KAIRIX_CONFIG_PATH", str(config_file))
        from kairix.core.search import config_loader

        config_loader._load_cached.cache_clear()
        with pytest.raises(ConfigValidationError):
            load_config()


@pytest.mark.unit
class TestLoadConfig:
    @pytest.mark.unit
    def test_returns_defaults_when_no_file(self, tmp_path, monkeypatch):
        monkeypatch.delenv("KAIRIX_CONFIG_PATH", raising=False)
        monkeypatch.chdir(tmp_path)
        # Clear lru_cache so path is re-resolved
        from kairix.core.search import config_loader

        config_loader._load_cached.cache_clear()
        cfg = load_config()
        assert isinstance(cfg, RetrievalConfig)

    @pytest.mark.unit
    def test_loads_from_env_var(self, tmp_path, monkeypatch):
        pytest.importorskip("yaml")
        config_file = tmp_path / "my-kairix.yaml"
        config_file.write_text(
            textwrap.dedent("""
            retrieval:
              boosts:
                entity:
                  enabled: false
        """)
        )
        monkeypatch.setenv("KAIRIX_CONFIG_PATH", str(config_file))
        from kairix.core.search import config_loader

        config_loader._load_cached.cache_clear()
        cfg = load_config()
        assert cfg.entity.enabled is False

    @pytest.mark.unit
    def test_invalid_yaml_falls_back_to_defaults(self, tmp_path, monkeypatch):
        """Malformed YAML falls back to defaults (not a validation error)."""
        config_file = tmp_path / "bad.yaml"
        config_file.write_text("{{{{invalid yaml content::::")
        monkeypatch.setenv("KAIRIX_CONFIG_PATH", str(config_file))
        from kairix.core.search import config_loader

        config_loader._load_cached.cache_clear()
        cfg = load_config()
        defaults = RetrievalConfig.defaults()
        assert cfg.entity.enabled == defaults.entity.enabled

    @pytest.mark.unit
    def test_env_path_nonexistent_falls_back(self, tmp_path, monkeypatch):
        """KAIRIX_CONFIG_PATH pointing to nonexistent file falls back to defaults."""
        monkeypatch.setenv("KAIRIX_CONFIG_PATH", str(tmp_path / "missing.yaml"))
        from kairix.core.search import config_loader

        config_loader._load_cached.cache_clear()
        cfg = load_config()
        assert isinstance(cfg, RetrievalConfig)


@pytest.mark.unit
class TestResolveConfigPath:
    @pytest.mark.unit
    def test_returns_none_when_no_file(self, tmp_path, monkeypatch):
        monkeypatch.delenv("KAIRIX_CONFIG_PATH", raising=False)
        monkeypatch.chdir(tmp_path)
        result = _resolve_config_path()
        assert result is None

    @pytest.mark.unit
    def test_returns_env_path_when_file_exists(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.yaml"
        config_file.write_text("retrieval: {}")
        monkeypatch.setenv("KAIRIX_CONFIG_PATH", str(config_file))
        result = _resolve_config_path()
        assert result == config_file

    @pytest.mark.unit
    def test_returns_none_when_env_path_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KAIRIX_CONFIG_PATH", str(tmp_path / "nope.yaml"))
        result = _resolve_config_path()
        assert result is None

    @pytest.mark.unit
    def test_finds_cwd_config(self, tmp_path, monkeypatch):
        monkeypatch.delenv("KAIRIX_CONFIG_PATH", raising=False)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "kairix.config.yaml").write_text("retrieval: {}")
        result = _resolve_config_path()
        assert result is not None
        assert result.name == "kairix.config.yaml"


@pytest.mark.unit
class TestParseCollections:
    @pytest.mark.unit
    def test_returns_none_when_not_present(self):
        result = parse_collections({})
        assert result is None

    @pytest.mark.unit
    def test_parses_shared_collections(self):
        data = {
            "collections": {
                "shared": [
                    {"name": "docs", "path": "documents", "glob": "**/*.txt"},
                    {"name": "wiki", "path": "wiki"},
                ],
            }
        }
        result = parse_collections(data)
        assert result is not None
        assert len(result.shared) == 2
        assert result.shared[0].name == "docs"
        assert result.shared[0].path == "documents"
        assert result.shared[0].glob == "**/*.txt"
        assert result.shared[1].glob == "**/*.md"  # default

    @pytest.mark.unit
    def test_parses_agent_pattern(self):
        data = {
            "collections": {
                "shared": [],
                "agent_pattern": "{agent}-docs",
            }
        }
        result = parse_collections(data)
        assert result is not None
        assert result.agent_pattern == "{agent}-docs"

    @pytest.mark.unit
    def test_parses_agent_paths(self):
        data = {
            "collections": {
                "shared": [],
                "agent_paths": {"shape": "/data/shape", "builder": "/data/builder"},
            }
        }
        result = parse_collections(data)
        assert result is not None
        assert result.agent_paths["shape"] == "/data/shape"

    @pytest.mark.unit
    def test_skips_invalid_shared_items(self):
        """Items without 'name' key are skipped."""
        data = {
            "collections": {
                "shared": [
                    {"path": "no_name"},  # missing name
                    {"name": "valid", "path": "ok"},
                ],
            }
        }
        result = parse_collections(data)
        assert result is not None
        assert len(result.shared) == 1
        assert result.shared[0].name == "valid"

    @pytest.mark.unit
    def test_returns_none_when_collections_empty(self):
        result = parse_collections({"collections": None})
        assert result is None


@pytest.mark.unit
class TestFusionStrategy:
    @pytest.mark.unit
    def test_unknown_fusion_strategy_falls_back(self):
        cfg = _parse_config({"retrieval": {"fusion_strategy": "unknown_strategy"}})
        assert cfg.fusion_strategy == RetrievalConfig.defaults().fusion_strategy

    @pytest.mark.unit
    def test_rrf_fusion_strategy_accepted(self):
        cfg = _parse_config({"retrieval": {"fusion_strategy": "rrf"}})
        assert cfg.fusion_strategy == "rrf"

    @pytest.mark.unit
    def test_custom_rrf_k(self):
        cfg = _parse_config({"retrieval": {"rrf_k": 30}})
        assert cfg.rrf_k == 30


@pytest.mark.unit
class TestLoadCachedEdgeCases:
    @pytest.mark.unit
    def test_none_path_returns_defaults(self):
        """_load_cached(None) returns defaults."""
        from kairix.core.search import config_loader

        config_loader._load_cached.cache_clear()
        cfg = _load_cached(None)
        assert isinstance(cfg, RetrievalConfig)

    @pytest.mark.unit
    def test_yaml_not_installed_falls_back(self, tmp_path, monkeypatch):
        """When PyYAML is not installed, falls back to defaults."""
        from kairix.core.search import config_loader

        config_loader._load_cached.cache_clear()
        config_file = tmp_path / "test.yaml"
        config_file.write_text("retrieval: {}")

        import builtins

        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("mocked")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)
        cfg = _load_cached(config_file)
        assert isinstance(cfg, RetrievalConfig)

    @pytest.mark.unit
    def test_parse_exception_falls_back(self, tmp_path):
        """Parse exception (not ConfigValidationError) falls back to defaults."""
        from unittest.mock import patch

        from kairix.core.search import config_loader

        config_loader._load_cached.cache_clear()
        config_file = tmp_path / "test2.yaml"
        config_file.write_text("retrieval:\n  boosts:\n    entity:\n      enabled: true\n")

        with patch(
            "kairix.core.search.config_loader._parse_config",
            side_effect=TypeError("bad parse"),
        ):
            cfg = _load_cached(config_file)
        assert isinstance(cfg, RetrievalConfig)


# ---------------------------------------------------------------------------
# Branch coverage — load_collections file loading + merge branches
# ---------------------------------------------------------------------------


@pytest.fixture
def kairix_config_file(tmp_path):
    """Write a kairix.config.yaml at a temp path and point KAIRIX_CONFIG_PATH at it.

    Uses direct ``os.environ`` manipulation rather than ``monkeypatch.setenv`` —
    KAIRIX_CONFIG_PATH is operator-facing config, not a code-level test seam.
    """
    import os

    config_path = tmp_path / "kairix.config.yaml"
    config_path.write_text(
        """
collections:
  shared:
    - name: home
      path: 00-Home
      glob: "**/*.md"
    - name: archive
      path: 06-Archive
      glob: "**/*.md"
      in_default: false
      retrieval:
        bm25_limit: 25
        boosts:
          entity:
            factor: 2.5
""",
        encoding="utf-8",
    )
    prev = os.environ.get("KAIRIX_CONFIG_PATH")
    os.environ["KAIRIX_CONFIG_PATH"] = str(config_path)
    yield config_path
    if prev is None:
        os.environ.pop("KAIRIX_CONFIG_PATH", None)
    else:
        os.environ["KAIRIX_CONFIG_PATH"] = prev


@pytest.mark.unit
def test_load_collections_returns_parsed_config_when_file_exists(kairix_config_file):
    """``load_collections`` reads the YAML and returns a populated ``CollectionsConfig``."""
    from kairix.core.search.config_loader import load_collections

    cfg = load_collections()
    assert cfg is not None
    names = {c.name for c in cfg.shared}
    assert names == {"home", "archive"}
    archive = next(c for c in cfg.shared if c.name == "archive")
    assert archive.in_default is False
    assert archive.retrieval_overrides == {
        "bm25_limit": 25,
        "boosts": {"entity": {"factor": 2.5}},
    }


@pytest.mark.unit
def test_load_collections_returns_none_when_yaml_unparseable(tmp_path):
    """When the configured path is unparseable YAML, ``load_collections`` returns None.

    Closes coverage of the ``except Exception: return None`` branch.
    """
    import os

    yaml_garbage = tmp_path / "garbage.yaml"
    # Conflicting block-mapping keys make yaml.safe_load raise.
    yaml_garbage.write_text("collections:\n  shared:\n    - name: x\n  - bogus_top: bad\n", encoding="utf-8")
    prev = os.environ.get("KAIRIX_CONFIG_PATH")
    os.environ["KAIRIX_CONFIG_PATH"] = str(yaml_garbage)
    try:
        from kairix.core.search.config_loader import load_collections

        cfg = load_collections()
    finally:
        if prev is None:
            os.environ.pop("KAIRIX_CONFIG_PATH", None)
        else:
            os.environ["KAIRIX_CONFIG_PATH"] = prev
    assert cfg is None


@pytest.mark.unit
def test_get_collection_overrides_returns_only_collections_with_overrides(kairix_config_file):
    """``_get_collection_overrides`` returns the per-collection override dicts."""
    from kairix.core.search.config_loader import _get_collection_overrides

    overrides = _get_collection_overrides()
    # Only 'archive' has retrieval_overrides in the fixture; 'home' has none.
    assert set(overrides.keys()) == {"archive"}
    assert overrides["archive"] == {
        "bm25_limit": 25,
        "boosts": {"entity": {"factor": 2.5}},
    }


@pytest.mark.unit
def test_get_collection_overrides_returns_empty_when_no_config_loaded():
    """When no kairix.config.yaml is found, the override map is empty."""
    import os

    prev = os.environ.pop("KAIRIX_CONFIG_PATH", None)
    try:
        from kairix.core.search.config_loader import _get_collection_overrides

        overrides = _get_collection_overrides()
        assert overrides == {}
    finally:
        if prev is not None:
            os.environ["KAIRIX_CONFIG_PATH"] = prev


@pytest.mark.unit
def test_merge_retrieval_config_applies_temporal_boost_overrides():
    """A ``boosts.temporal`` override merges with the base config's temporal sub-config.

    Uses the same nested shape ``_parse_temporal`` consumes from full-config
    YAML (``date_path_boost: {factor, ...}``) so per-collection overrides
    look identical to top-level defaults.
    """
    from kairix.core.search.config_loader import _merge_retrieval_config

    base = RetrievalConfig()
    overrides = {
        "boosts": {
            "temporal": {
                "date_path_boost": {
                    "factor": 5.0,
                    "recency_window_days": 14,
                }
            }
        }
    }
    merged = _merge_retrieval_config(base, overrides)
    # Specifically-overridden values are applied.
    assert merged.temporal.date_path_boost_factor == 5.0
    assert merged.temporal.date_path_recency_window_days == 14
    # Non-overridden values within the same sub-block fall back to BASE — not
    # to the parser's hard defaults. This was the bug surfaced by adding the
    # test: the previous merge produced flat-key dicts the parser couldn't
    # read, so non-overridden fields silently returned hard defaults.
    assert merged.temporal.date_path_boost_enabled == base.temporal.date_path_boost_enabled
    # And the entirely-untouched sub-block stays at base.
    assert merged.temporal.chunk_date_boost_enabled == base.temporal.chunk_date_boost_enabled
    assert merged.temporal.chunk_date_decay_halflife_days == base.temporal.chunk_date_decay_halflife_days
    # Other top-level fields untouched.
    assert merged.fusion_strategy == base.fusion_strategy


@pytest.mark.unit
def test_merge_retrieval_config_applies_rerank_overrides():
    """A ``rerank`` override merges with the base config's rerank sub-config."""
    from kairix.core.search.config_loader import _merge_retrieval_config

    base = RetrievalConfig()
    overrides = {"rerank": {"enabled": True, "candidate_limit": 50}}
    merged = _merge_retrieval_config(base, overrides)
    assert merged.rerank.enabled is True
    assert merged.rerank.candidate_limit == 50
    # Non-overridden rerank sub-field (model) keeps base default.
    assert merged.rerank.model == base.rerank.model
