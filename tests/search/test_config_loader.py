"""Tests for kairix YAML config loader.

All env-driven resolution goes through the documented ``env=`` and
``config_path=`` test seams (F2-clean alternatives to monkey-patching
``KAIRIX_CONFIG_PATH``). The single ``tmp_path`` chdir per test prevents
the cwd-fallback from picking up a stray ``kairix.config.yaml``.
"""

from __future__ import annotations

import sys
import textwrap

import pytest

from kairix.core.search.config import RetrievalConfig
from kairix.core.search.config_loader import (
    ConfigValidationError,
    load_cached,
    load_config,
    parse_collections,
    parse_config,
    resolve_config_path,
    validate_config,
)


@pytest.mark.unit
class TestParseConfig:
    @pytest.mark.unit
    def test_empty_dict_returns_defaults(self):
        cfg = parse_config({})
        defaults = RetrievalConfig.defaults()
        assert cfg.entity.enabled == defaults.entity.enabled
        assert cfg.procedural.factor == defaults.procedural.factor

    @pytest.mark.unit
    def test_entity_enabled_false(self):
        cfg = parse_config({"retrieval": {"boosts": {"entity": {"enabled": False}}}})
        assert cfg.entity.enabled is False

    @pytest.mark.unit
    def test_procedural_custom_factor(self):
        cfg = parse_config({"retrieval": {"boosts": {"procedural": {"factor": 1.8}}}})
        assert cfg.procedural.factor == pytest.approx(1.8)

    @pytest.mark.unit
    def test_custom_path_patterns(self):
        cfg = parse_config({"retrieval": {"boosts": {"procedural": {"path_patterns": [r"(?:^|/)docs/"]}}}})
        assert r"(?:^|/)docs/" in cfg.procedural.path_patterns

    @pytest.mark.unit
    def test_temporal_date_path_boost_enabled(self):
        cfg = parse_config(
            {"retrieval": {"boosts": {"temporal": {"date_path_boost": {"enabled": True, "factor": 1.5}}}}}
        )
        assert cfg.temporal.date_path_boost_enabled is True
        assert cfg.temporal.date_path_boost_factor == pytest.approx(1.5)

    @pytest.mark.unit
    def test_temporal_chunk_date_boost_enabled(self):
        cfg = parse_config(
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
        cfg = parse_config({})
        assert cfg.temporal.chunk_date_boost_guard_explicit_only is True

    @pytest.mark.unit
    def test_temporal_chunk_date_guard_explicit_only_can_disable(self):
        cfg = parse_config(
            {"retrieval": {"boosts": {"temporal": {"chunk_date_boost": {"guard_explicit_only": False}}}}}
        )
        assert cfg.temporal.chunk_date_boost_guard_explicit_only is False

    @pytest.mark.unit
    def test_rerank_config_parsed(self):
        cfg = parse_config({"retrieval": {"rerank": {"enabled": True, "candidate_limit": 30}}})
        assert cfg.rerank.enabled is True
        assert cfg.rerank.candidate_limit == 30

    @pytest.mark.unit
    def test_rerank_defaults_disabled(self):
        cfg = parse_config({})
        assert cfg.rerank.enabled is False


@pytest.mark.unit
class TestValidateConfig:
    @pytest.mark.unit
    def test_valid_defaults_pass(self):
        cfg = parse_config({})
        # Contract: validate_config returns None and does not raise on a
        # well-formed default config. Pin the documented return type so a
        # future refactor that adds a return-value contract can't silently
        # change behaviour (replaces a tautological ``assert True``; S5914).
        assert validate_config(cfg) is None

    @pytest.mark.unit
    def test_entity_factor_out_of_range_raises(self):
        cfg = parse_config({"retrieval": {"boosts": {"entity": {"factor": 99.0}}}})
        with pytest.raises(ConfigValidationError, match=r"entity\.factor"):
            validate_config(cfg)

    @pytest.mark.unit
    def test_entity_cap_below_min_raises(self):
        cfg = parse_config({"retrieval": {"boosts": {"entity": {"cap": 0.5}}}})
        with pytest.raises(ConfigValidationError, match=r"entity\.cap"):
            validate_config(cfg)

    @pytest.mark.unit
    def test_procedural_factor_out_of_range_raises(self):
        cfg = parse_config({"retrieval": {"boosts": {"procedural": {"factor": 0.5}}}})
        with pytest.raises(ConfigValidationError, match=r"procedural\.factor"):
            validate_config(cfg)

    @pytest.mark.unit
    def test_multiple_errors_reported_together(self):
        cfg = parse_config(
            {
                "retrieval": {
                    "boosts": {
                        "entity": {"factor": 99.0, "cap": 0.1},
                    }
                }
            }
        )
        with pytest.raises(ConfigValidationError) as exc_info:
            validate_config(cfg)
        msg = str(exc_info.value)
        assert "entity.factor" in msg
        assert "entity.cap" in msg

    @pytest.mark.unit
    def test_invalid_config_not_silently_swallowed(self, tmp_path):
        """ConfigValidationError must propagate — never fall back to defaults on invalid config."""
        pytest.importorskip("yaml", reason="config loader uses PyYAML; skip when not installed (optional via [dev])")
        config_file = tmp_path / "kairix.config.yaml"
        config_file.write_text(
            textwrap.dedent("""
            retrieval:
              boosts:
                entity:
                  factor: 999.0
        """)
        )
        from kairix.core.search import config_loader

        config_loader.load_cached.cache_clear()
        with pytest.raises(ConfigValidationError):
            load_config(config_path=config_file)


@pytest.mark.unit
class TestLoadConfig:
    @pytest.mark.unit
    def test_returns_defaults_when_no_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # Clear lru_cache so path is re-resolved
        from kairix.core.search import config_loader

        config_loader.reset_config_cache()
        cfg = load_config(env={})
        assert isinstance(cfg, RetrievalConfig)

    @pytest.mark.unit
    def test_loads_from_env_var(self, tmp_path):
        pytest.importorskip("yaml", reason="config loader uses PyYAML; skip when not installed (optional via [dev])")
        config_file = tmp_path / "my-kairix.yaml"
        config_file.write_text(
            textwrap.dedent("""
            retrieval:
              boosts:
                entity:
                  enabled: false
        """)
        )
        from kairix.core.search import config_loader

        config_loader.reset_config_cache()
        cfg = load_config(env={"KAIRIX_CONFIG_PATH": str(config_file)})
        assert cfg.entity.enabled is False

    @pytest.mark.unit
    def test_invalid_yaml_falls_back_to_defaults(self, tmp_path):
        """Malformed YAML falls back to defaults (not a validation error)."""
        config_file = tmp_path / "bad.yaml"
        config_file.write_text("{{{{invalid yaml content::::")
        from kairix.core.search import config_loader

        config_loader.reset_config_cache()
        cfg = load_config(env={"KAIRIX_CONFIG_PATH": str(config_file)})
        defaults = RetrievalConfig.defaults()
        assert cfg.entity.enabled == defaults.entity.enabled

    @pytest.mark.unit
    def test_env_path_nonexistent_falls_back(self, tmp_path):
        """KAIRIX_CONFIG_PATH pointing to nonexistent file falls back to defaults."""
        from kairix.core.search import config_loader

        config_loader.reset_config_cache()
        cfg = load_config(env={"KAIRIX_CONFIG_PATH": str(tmp_path / "missing.yaml")})
        assert isinstance(cfg, RetrievalConfig)


@pytest.mark.unit
class TestResolveConfigPath:
    @pytest.mark.unit
    def test_returns_none_when_no_file(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = resolve_config_path()
        assert result is None

    @pytest.mark.unit
    def test_returns_env_path_when_file_exists(self, tmp_path):
        """Explicit ``config_path=`` arg is the F2-clean alternative to the env var."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("retrieval: {}")
        result = resolve_config_path(explicit=config_file)
        assert result == config_file

    @pytest.mark.unit
    def test_returns_none_when_env_path_missing(self, tmp_path):
        """Explicit path pointing at a missing file returns None — same
        contract as the env-var path."""
        result = resolve_config_path(explicit=tmp_path / "nope.yaml")
        assert result is None

    @pytest.mark.unit
    def test_finds_cwd_config(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "kairix.config.yaml").write_text("retrieval: {}")
        result = resolve_config_path()
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
    def test_in_default_true_parses_onto_collection_def(self):
        """An explicit ``in_default: true`` round-trips onto the CollectionDef."""
        data = {"collections": {"shared": [{"name": "home", "path": "home", "in_default": True}]}}
        result = parse_collections(data)
        assert result is not None
        assert result.shared[0].in_default is True

    @pytest.mark.unit
    def test_in_default_defaults_to_true_when_omitted(self):
        """A collection without ``in_default`` keeps the back-compat default."""
        data = {"collections": {"shared": [{"name": "home", "path": "home"}]}}
        result = parse_collections(data)
        assert result is not None
        assert result.shared[0].in_default is True

    @pytest.mark.unit
    def test_non_bool_in_default_is_rejected_at_parse_time(self):
        """A non-boolean ``in_default`` value raises ConfigValidationError
        naming the offending key — ``_coerce_bool`` rejects the
        ``"false"``-as-string footgun rather than silently coercing to
        True (which would route the collection into the opposite scope).
        """
        data = {"collections": {"shared": [{"name": "archive", "path": "archive", "in_default": "false-as-string"}]}}
        with pytest.raises(ConfigValidationError, match="in_default"):
            parse_collections(data)


@pytest.mark.unit
class TestFusionStrategy:
    @pytest.mark.unit
    def test_unknown_fusion_strategy_falls_back(self):
        cfg = parse_config({"retrieval": {"fusion_strategy": "unknown_strategy"}})
        assert cfg.fusion_strategy == RetrievalConfig.defaults().fusion_strategy

    @pytest.mark.unit
    def test_rrf_fusion_strategy_accepted(self):
        cfg = parse_config({"retrieval": {"fusion_strategy": "rrf"}})
        assert cfg.fusion_strategy == "rrf"

    @pytest.mark.unit
    def test_custom_rrf_k(self):
        cfg = parse_config({"retrieval": {"rrf_k": 30}})
        assert cfg.rrf_k == 30


@pytest.mark.unit
class TestLoadCachedEdgeCases:
    @pytest.mark.unit
    def test_none_path_returns_defaults(self):
        """load_cached(None) returns defaults."""
        from kairix.core.search import config_loader

        config_loader.load_cached.cache_clear()
        cfg = load_cached(None)
        assert isinstance(cfg, RetrievalConfig)

    @pytest.mark.unit
    def test_yaml_not_installed_falls_back(self, tmp_path, monkeypatch):
        """When PyYAML is not installed, falls back to defaults.

        PyYAML is simulated as not installed with a ``None`` ``sys.modules``
        entry (auto-undone by monkeypatch) — no import hook is patched.
        """
        from kairix.core.search import config_loader

        config_loader.load_cached.cache_clear()
        config_file = tmp_path / "test.yaml"
        # A non-default value: if the file were parsed, rrf_k would be 7.
        config_file.write_text("retrieval:\n  rrf_k: 7\n")

        # A None entry in sys.modules makes ``import <dep>`` raise ImportError —
        # the optional dependency is simulated as not installed (auto-undone).
        monkeypatch.setitem(sys.modules, "yaml", None)
        cfg = load_cached(config_file)
        assert cfg == RetrievalConfig.defaults(), "without PyYAML the file must not be parsed — defaults only"

    @pytest.mark.unit
    def test_parse_exception_falls_back(self, tmp_path):
        """Parse exception (not ConfigValidationError) falls back to defaults.

        Drives the path by writing a YAML value that ``parse_config`` will
        choke on naturally (``rrf_k`` as a list raises ``TypeError`` inside
        ``int(...)``) — no internal @patch needed.
        """
        from kairix.core.search import config_loader

        config_loader.load_cached.cache_clear()
        config_file = tmp_path / "test2.yaml"
        config_file.write_text("retrieval:\n  rrf_k: [1, 2, 3]\n")

        cfg = load_cached(config_file)
        assert isinstance(cfg, RetrievalConfig)


# ---------------------------------------------------------------------------
# #458 + #455 + #432 YAML loader wiring — operator overlay parsers
# ---------------------------------------------------------------------------


class TestContentQualityBoostYaml:
    @pytest.mark.unit
    def test_content_quality_boost_parsed_from_yaml(self, tmp_path):
        """``retrieval.boosts.content_quality.enabled: true`` flows through
        to ``RetrievalConfig.content_quality_boost.enabled`` so the
        factory wires the boost into the chain.

        Sabotage-proof: drop the ``_parse_content_quality_boost`` call
        in ``load_config`` and the assertion below fails — config defaults
        to disabled.
        """
        from kairix.core.search.config_loader import load_config

        cfg_file = tmp_path / "kairix.config.yaml"
        cfg_file.write_text(
            textwrap.dedent(
                """
                provider: fake
                retrieval:
                  boosts:
                    content_quality:
                      enabled: true
                      length_substantive_ceiling: 1.3
                      structure_ceiling: 1.25
                """
            ).lstrip()
        )
        cfg = load_config(cfg_file)
        assert cfg.content_quality_boost.enabled is True
        assert cfg.content_quality_boost.length_substantive_ceiling == 1.3
        assert cfg.content_quality_boost.structure_ceiling == 1.25

    @pytest.mark.unit
    def test_content_quality_boost_absent_block_keeps_defaults(self, tmp_path):
        """When the operator omits the ``content_quality`` block, the
        loader keeps the default-disabled config so existing deployments
        see byte-for-byte pre-#458 behaviour."""
        from kairix.core.search.config_loader import load_config

        cfg_file = tmp_path / "kairix.config.yaml"
        cfg_file.write_text("provider: fake\nretrieval: {}\n")
        cfg = load_config(cfg_file)
        assert cfg.content_quality_boost.enabled is False


class TestFusionFloorYaml:
    @pytest.mark.unit
    def test_fact_and_chunk_floors_parsed_from_yaml(self, tmp_path):
        """#455 — operator-supplied fact / chunk floors flow through to
        the constructed :class:`RetrievalConfig`. Both default to 0.0
        if absent.

        Sabotage-proof: drop the ``fact_layer_min_floor`` line from
        ``load_config`` and the floor stays at 0.0 even when the YAML
        sets 0.4.
        """
        from kairix.core.search.config_loader import load_config

        cfg_file = tmp_path / "kairix.config.yaml"
        cfg_file.write_text(
            textwrap.dedent(
                """
                provider: fake
                retrieval:
                  fact_layer_min_floor: 0.4
                  chunk_layer_min_floor: 0.3
                """
            ).lstrip()
        )
        cfg = load_config(cfg_file)
        assert cfg.fact_layer_min_floor == 0.4
        assert cfg.chunk_layer_min_floor == 0.3

    @pytest.mark.unit
    def test_cross_layer_dedup_flag_parsed_from_yaml(self, tmp_path):
        from kairix.core.search.config_loader import load_config

        cfg_file = tmp_path / "kairix.config.yaml"
        cfg_file.write_text("provider: fake\nretrieval:\n  cross_layer_dedup_enabled: true\n")
        cfg = load_config(cfg_file)
        assert cfg.cross_layer_dedup_enabled is True

    @pytest.mark.unit
    def test_floors_default_to_zero_when_absent(self, tmp_path):
        """All three #455 knobs default to no-op when the YAML omits them."""
        from kairix.core.search.config_loader import load_config

        cfg_file = tmp_path / "kairix.config.yaml"
        cfg_file.write_text("provider: fake\nretrieval: {}\n")
        cfg = load_config(cfg_file)
        assert cfg.fact_layer_min_floor == 0.0
        assert cfg.chunk_layer_min_floor == 0.0
        assert cfg.cross_layer_dedup_enabled is False


class TestSourceTierBoostYaml:
    @pytest.mark.unit
    def test_source_tier_boost_parsed_from_yaml(self, tmp_path):
        """#432 — ``retrieval.boosts.source_tier.enabled: true`` flows
        through so the factory wires :class:`SourceTierBoost` into the
        chain."""
        from kairix.core.search.config_loader import load_config

        cfg_file = tmp_path / "kairix.config.yaml"
        cfg_file.write_text(
            textwrap.dedent(
                """
                provider: fake
                retrieval:
                  boosts:
                    source_tier:
                      enabled: true
                """
            ).lstrip()
        )
        cfg = load_config(cfg_file)
        assert cfg.source_tier_boost.enabled is True

    @pytest.mark.unit
    def test_source_tier_boost_absent_block_keeps_defaults(self, tmp_path):
        from kairix.core.search.config_loader import load_config

        cfg_file = tmp_path / "kairix.config.yaml"
        cfg_file.write_text("provider: fake\nretrieval: {}\n")
        cfg = load_config(cfg_file)
        assert cfg.source_tier_boost.enabled is False

    @pytest.mark.unit
    def test_source_tier_yaml_loader_parses_allowlist_and_overrides(self, tmp_path):
        """End-to-end YAML → SourceTierBoostConfig: the loader honours
        ``canonical_filename_allowlist`` + ``per_intent_overrides`` (#432)."""
        from kairix.core.search.config import SourceTier
        from kairix.core.search.config_loader import load_config

        cfg_file = tmp_path / "kairix.config.yaml"
        cfg_file.write_text(
            textwrap.dedent(
                """
                provider: fake
                retrieval:
                  boosts:
                    source_tier:
                      enabled: true
                      canonical_filename_allowlist:
                        - ETHOS.md
                        - AGENTS.md
                      per_intent_overrides:
                        - intent: entity
                          tier: canonical
                          multiplier: 5.0
                        - intent: procedural
                          tier: active_standard
                          multiplier: 3.0
                """
            ).lstrip()
        )
        cfg = load_config(cfg_file)
        stb = cfg.source_tier_boost
        assert stb.enabled is True
        assert stb.canonical_filename_allowlist == ("ETHOS.md", "AGENTS.md")
        assert ("entity", SourceTier.CANONICAL, 5.0) in stb.per_intent_overrides
        assert ("procedural", SourceTier.ACTIVE_STANDARD, 3.0) in stb.per_intent_overrides

    @pytest.mark.unit
    def test_source_tier_yaml_loader_skips_malformed_override_entries(self, tmp_path, caplog):
        """A malformed override entry (missing field / unknown tier /
        bad multiplier) is skipped with a warning — one typo doesn't
        break the whole config."""
        import logging

        from kairix.core.search.config_loader import load_config

        cfg_file = tmp_path / "kairix.config.yaml"
        cfg_file.write_text(
            textwrap.dedent(
                """
                provider: fake
                retrieval:
                  boosts:
                    source_tier:
                      enabled: true
                      per_intent_overrides:
                        - intent: entity
                          tier: not-a-real-tier
                          multiplier: 5.0
                        - intent: procedural
                          tier: canonical
                          multiplier: notanumber
                        - tier: canonical
                          multiplier: 4.0
                        - intent: temporal
                          tier: canonical
                          multiplier: 4.0
                """
            ).lstrip()
        )
        with caplog.at_level(logging.WARNING):
            cfg = load_config(cfg_file)
        # Only the well-formed entry survived.
        assert len(cfg.source_tier_boost.per_intent_overrides) == 1
        assert cfg.source_tier_boost.per_intent_overrides[0][0] == "temporal"


class TestTopologyBackedCollectionOverrides:
    """Per-collection retrieval overrides now flow from the canonical
    topology (``topology.collections[*].retrieval``) rather than the
    legacy ``collections.shared[*].retrieval`` block (canonical-collapse
    T3). These probes drive ``resolve_retrieval_config`` with its
    ``overrides_fn`` bound to the topology producer so the override is
    proven to originate from a topology collection.
    """

    @pytest.mark.unit
    def test_topology_override_merges_over_global_for_single_collection(self) -> None:
        """A ``retrieval:`` block on a topology collection is merged over
        the global config when a single-collection search names it."""
        from kairix.core.factory import derive_collection_overrides
        from kairix.core.search.config_loader import ResolveConfigDeps, resolve_retrieval_config

        topology = {
            "topology": {
                "collections": [
                    {
                        "name": "reflib",
                        "sources": [{"cc_pair": "obs-cp", "path_filter": "*"}],
                        "retrieval": {"fusion_strategy": "bm25_primary", "vec_limit": 5},
                    }
                ]
            }
        }
        global_cfg = RetrievalConfig(fusion_strategy="rrf", vec_limit=20)

        resolved = resolve_retrieval_config(
            collection="reflib",
            deps=ResolveConfigDeps(
                config_fn=lambda: global_cfg,
                overrides_fn=lambda: derive_collection_overrides(mapping=topology),
            ),
        )

        # Topology override wins over the global config.
        assert resolved.fusion_strategy == "bm25_primary"
        assert resolved.vec_limit == 5

    @pytest.mark.unit
    def test_topology_override_absent_collection_keeps_global(self) -> None:
        """A collection with no topology ``retrieval:`` block keeps the
        global config — the override is keyed on the collection name."""
        from kairix.core.factory import derive_collection_overrides
        from kairix.core.search.config_loader import ResolveConfigDeps, resolve_retrieval_config

        topology = {
            "topology": {
                "collections": [
                    {
                        "name": "reflib",
                        "sources": [{"cc_pair": "obs-cp", "path_filter": "*"}],
                        "retrieval": {"fusion_strategy": "bm25_primary"},
                    }
                ]
            }
        }
        global_cfg = RetrievalConfig(fusion_strategy="rrf")

        resolved = resolve_retrieval_config(
            collection="team-scratch",
            deps=ResolveConfigDeps(
                config_fn=lambda: global_cfg,
                overrides_fn=lambda: derive_collection_overrides(mapping=topology),
            ),
        )

        assert resolved.fusion_strategy == "rrf"

    @pytest.mark.unit
    def test_default_overrides_fn_binds_callable_returning_dict(self) -> None:
        """The production ``ResolveConfigDeps`` default wires a working
        topology-backed override producer — a callable that returns a dict
        (the canonical-collapse producer is default-safe, so an empty
        config yields ``{}`` rather than raising).

        Sabotage proof: the retired legacy ``_get_collection_overrides``
        loader is gone; the default factory now binds the topology
        producer, which stays callable here.
        """
        from kairix.core.search.config_loader import ResolveConfigDeps

        deps = ResolveConfigDeps()
        assert callable(deps.overrides_fn)
        result = deps.overrides_fn()
        assert isinstance(result, dict)
