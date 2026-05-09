"""Tests for kairix YAML config loader.

Every test drives behaviour through the public surface — ``load_config()``,
``load_collections()``, ``resolve_retrieval_config()``, ``parse_collections()``.
Private helpers (``_parse_config``, ``_validate_config``, ``_resolve_config_path``,
``_load_cached``, ``_get_collection_overrides``, ``_merge_retrieval_config``) are
not imported; their behaviour is observed via the returned ``RetrievalConfig`` /
``CollectionsConfig`` shape and the raised ``ConfigValidationError``.

Tests pass an explicit ``env={"KAIRIX_CONFIG_PATH": ...}`` mapping through
``load_config(env=...)`` rather than mutating ``os.environ`` — KAIRIX_CONFIG_PATH
is the operator-facing knob, but the test doesn't need to touch the process env
to drive it.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from kairix.core.search.config import RetrievalConfig
from kairix.core.search.config_loader import (
    ConfigValidationError,
    load_collections,
    load_config,
    parse_collections,
    resolve_retrieval_config,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def config_at_env_path(tmp_path: Path):
    """Write YAML to ``tmp_path/kairix.config.yaml`` and return a ``write(yaml)``
    callable. The path is exposed via ``write.path``, the matching env mapping
    via ``write.env``, and config_fn/overrides_fn lambdas via
    ``write.config_fn`` / ``write.overrides_fn`` so tests can pass them
    through ``resolve_retrieval_config(...)`` without process-env mutation.

    Clears the ``@lru_cache`` between writes so each test sees its own YAML.
    """
    from kairix.core.search import config_loader

    config_path = tmp_path / "kairix.config.yaml"
    env_mapping = {"KAIRIX_CONFIG_PATH": str(config_path)}

    def write(yaml_text: str) -> Path:
        config_path.write_text(textwrap.dedent(yaml_text).lstrip(), encoding="utf-8")
        config_loader._load_cached.cache_clear()
        return config_path

    def _config_fn() -> RetrievalConfig:
        return load_config(env=env_mapping)

    def _overrides_fn() -> dict[str, dict]:
        cfg = load_collections(env=env_mapping)
        if not cfg:
            return {}
        return {c.name: c.retrieval_overrides for c in cfg.shared if c.retrieval_overrides}

    write.path = config_path  # type: ignore[attr-defined]
    write.env = env_mapping  # type: ignore[attr-defined]
    write.config_fn = _config_fn  # type: ignore[attr-defined]
    write.overrides_fn = _overrides_fn  # type: ignore[attr-defined]

    yield write
    config_loader._load_cached.cache_clear()


@pytest.fixture
def no_config_path(tmp_path: Path):
    """Yield an env+cwd pair guaranteeing no config is discovered: empty env
    (no KAIRIX_CONFIG_PATH) + a cwd that contains no kairix.config.yaml.
    """
    from kairix.core.search import config_loader

    config_loader._load_cached.cache_clear()
    yield {"env": {}, "cwd": tmp_path}
    config_loader._load_cached.cache_clear()


# ---------------------------------------------------------------------------
# load_config — YAML → RetrievalConfig parsing surface
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLoadConfigParsing:
    def test_no_yaml_at_env_path_returns_defaults(self, no_config_path) -> None:
        cfg = load_config(**no_config_path)
        defaults = RetrievalConfig.defaults()
        assert cfg.entity.enabled == defaults.entity.enabled
        assert cfg.procedural.factor == defaults.procedural.factor

    def test_empty_retrieval_block_returns_defaults(self, config_at_env_path) -> None:
        config_at_env_path("retrieval: {}\n")
        cfg = load_config(env=config_at_env_path.env)
        defaults = RetrievalConfig.defaults()
        assert cfg.entity.enabled == defaults.entity.enabled
        assert cfg.procedural.factor == defaults.procedural.factor

    def test_entity_enabled_can_be_disabled_via_yaml(self, config_at_env_path) -> None:
        config_at_env_path("""
            retrieval:
              boosts:
                entity:
                  enabled: false
        """)
        cfg = load_config(env=config_at_env_path.env)
        assert cfg.entity.enabled is False

    def test_procedural_factor_is_taken_from_yaml(self, config_at_env_path) -> None:
        config_at_env_path("""
            retrieval:
              boosts:
                procedural:
                  factor: 1.8
        """)
        cfg = load_config(env=config_at_env_path.env)
        assert cfg.procedural.factor == pytest.approx(1.8)

    def test_custom_path_patterns_replace_defaults(self, config_at_env_path) -> None:
        config_at_env_path("""
            retrieval:
              boosts:
                procedural:
                  path_patterns:
                    - "(?:^|/)docs/"
        """)
        cfg = load_config(env=config_at_env_path.env)
        assert r"(?:^|/)docs/" in cfg.procedural.path_patterns

    def test_temporal_date_path_boost_yaml_round_trip(self, config_at_env_path) -> None:
        config_at_env_path("""
            retrieval:
              boosts:
                temporal:
                  date_path_boost:
                    enabled: true
                    factor: 1.5
        """)
        cfg = load_config(env=config_at_env_path.env)
        assert cfg.temporal.date_path_boost_enabled is True
        assert cfg.temporal.date_path_boost_factor == pytest.approx(1.5)

    def test_temporal_chunk_date_boost_yaml_round_trip(self, config_at_env_path) -> None:
        config_at_env_path("""
            retrieval:
              boosts:
                temporal:
                  chunk_date_boost:
                    enabled: true
                    decay_halflife_days: 14
        """)
        cfg = load_config(env=config_at_env_path.env)
        assert cfg.temporal.chunk_date_boost_enabled is True
        assert cfg.temporal.chunk_date_decay_halflife_days == 14

    def test_chunk_date_guard_explicit_only_default_is_true(self, no_config_path) -> None:
        cfg = load_config(**no_config_path)
        assert cfg.temporal.chunk_date_boost_guard_explicit_only is True

    def test_chunk_date_guard_explicit_only_can_be_disabled_via_yaml(self, config_at_env_path) -> None:
        config_at_env_path("""
            retrieval:
              boosts:
                temporal:
                  chunk_date_boost:
                    guard_explicit_only: false
        """)
        cfg = load_config(env=config_at_env_path.env)
        assert cfg.temporal.chunk_date_boost_guard_explicit_only is False

    def test_rerank_block_yaml_round_trip(self, config_at_env_path) -> None:
        config_at_env_path("""
            retrieval:
              rerank:
                enabled: true
                candidate_limit: 30
        """)
        cfg = load_config(env=config_at_env_path.env)
        assert cfg.rerank.enabled is True
        assert cfg.rerank.candidate_limit == 30

    def test_rerank_default_is_disabled(self, no_config_path) -> None:
        cfg = load_config(**no_config_path)
        assert cfg.rerank.enabled is False


# ---------------------------------------------------------------------------
# Validation — out-of-range values raise ConfigValidationError; never silently default
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLoadConfigValidation:
    def test_entity_factor_out_of_range_raises(self, config_at_env_path) -> None:
        config_at_env_path("""
            retrieval:
              boosts:
                entity:
                  factor: 99.0
        """)
        with pytest.raises(ConfigValidationError, match=r"entity\.factor"):
            load_config(env=config_at_env_path.env)

    def test_entity_cap_below_min_raises(self, config_at_env_path) -> None:
        config_at_env_path("""
            retrieval:
              boosts:
                entity:
                  cap: 0.5
        """)
        with pytest.raises(ConfigValidationError, match=r"entity\.cap"):
            load_config(env=config_at_env_path.env)

    def test_procedural_factor_out_of_range_raises(self, config_at_env_path) -> None:
        config_at_env_path("""
            retrieval:
              boosts:
                procedural:
                  factor: 0.5
        """)
        with pytest.raises(ConfigValidationError, match=r"procedural\.factor"):
            load_config(env=config_at_env_path.env)

    def test_multiple_invalid_values_are_reported_together(self, config_at_env_path) -> None:
        config_at_env_path("""
            retrieval:
              boosts:
                entity:
                  factor: 99.0
                  cap: 0.1
        """)
        with pytest.raises(ConfigValidationError) as exc_info:
            load_config(env=config_at_env_path.env)
        msg = str(exc_info.value)
        assert "entity.factor" in msg
        assert "entity.cap" in msg

    def test_invalid_config_propagates_does_not_silently_fall_back(self, config_at_env_path) -> None:
        """A ConfigValidationError must surface; loaders never quietly use defaults on bad input."""
        config_at_env_path("""
            retrieval:
              boosts:
                entity:
                  factor: 999.0
        """)
        with pytest.raises(ConfigValidationError):
            load_config(env=config_at_env_path.env)


# ---------------------------------------------------------------------------
# Path resolution + caching — observed through load_config behaviour
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLoadConfigPathResolution:
    def test_no_env_var_and_no_cwd_file_returns_defaults(self, no_config_path) -> None:
        cfg = load_config(**no_config_path)
        assert isinstance(cfg, RetrievalConfig)

    def test_env_var_pointing_at_yaml_loads_that_file(self, config_at_env_path) -> None:
        config_at_env_path("""
            retrieval:
              boosts:
                entity:
                  enabled: false
        """)
        cfg = load_config(env=config_at_env_path.env)
        assert cfg.entity.enabled is False

    def test_env_var_pointing_at_missing_file_falls_back_to_defaults(self, tmp_path: Path) -> None:
        from kairix.core.search import config_loader

        config_loader._load_cached.cache_clear()
        cfg = load_config(env={"KAIRIX_CONFIG_PATH": str(tmp_path / "missing.yaml")})
        assert isinstance(cfg, RetrievalConfig)
        assert cfg.entity.enabled == RetrievalConfig.defaults().entity.enabled

    def test_kairix_config_yaml_in_cwd_is_picked_up_when_env_var_unset(self, tmp_path: Path) -> None:
        from kairix.core.search import config_loader

        (tmp_path / "kairix.config.yaml").write_text(
            textwrap.dedent("""
                retrieval:
                  boosts:
                    entity:
                      enabled: false
            """).lstrip(),
            encoding="utf-8",
        )
        config_loader._load_cached.cache_clear()
        cfg = load_config(env={}, cwd=tmp_path)
        assert cfg.entity.enabled is False

    def test_malformed_yaml_falls_back_to_defaults(self, config_at_env_path) -> None:
        """Unparseable YAML at the configured path → defaults (not validation error)."""
        config_at_env_path("{{{{invalid yaml content::::\n")
        cfg = load_config(env=config_at_env_path.env)
        assert cfg.entity.enabled == RetrievalConfig.defaults().entity.enabled

    def test_unexpected_parse_exception_falls_back_to_defaults(self, config_at_env_path) -> None:
        """A YAML mapping whose ``retrieval`` value is the wrong type triggers an
        AttributeError inside parsing — caught by the loader's catch-all → defaults.
        Exercises the ``except Exception`` branch without monkey-patching internals.
        """
        # A scalar value at retrieval: causes ``retrieval.get(...)`` to raise.
        config_at_env_path("retrieval: 5\n")
        cfg = load_config(env=config_at_env_path.env)
        assert cfg.entity.enabled == RetrievalConfig.defaults().entity.enabled


# ---------------------------------------------------------------------------
# parse_collections — public surface
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestParseCollections:
    def test_returns_none_when_collections_block_absent(self) -> None:
        assert parse_collections({}) is None

    def test_parses_shared_collections_with_default_glob(self) -> None:
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
        assert result.shared[1].glob == "**/*.md"

    def test_parses_agent_pattern(self) -> None:
        result = parse_collections({"collections": {"shared": [], "agent_pattern": "{agent}-docs"}})
        assert result is not None
        assert result.agent_pattern == "{agent}-docs"

    def test_parses_agent_paths(self) -> None:
        result = parse_collections(
            {
                "collections": {
                    "shared": [],
                    "agent_paths": {"shape": "/data/shape", "builder": "/data/builder"},
                }
            }
        )
        assert result is not None
        assert result.agent_paths["shape"] == "/data/shape"

    def test_skips_shared_items_missing_a_name(self) -> None:
        result = parse_collections(
            {
                "collections": {
                    "shared": [
                        {"path": "no_name"},
                        {"name": "valid", "path": "ok"},
                    ],
                }
            }
        )
        assert result is not None
        assert len(result.shared) == 1
        assert result.shared[0].name == "valid"

    def test_returns_none_when_collections_value_is_null(self) -> None:
        assert parse_collections({"collections": None}) is None


# ---------------------------------------------------------------------------
# Fusion strategy — driven through load_config
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFusionStrategy:
    def test_unknown_fusion_strategy_falls_back_to_default(self, config_at_env_path) -> None:
        config_at_env_path("""
            retrieval:
              fusion_strategy: unknown_strategy
        """)
        cfg = load_config(env=config_at_env_path.env)
        assert cfg.fusion_strategy == RetrievalConfig.defaults().fusion_strategy

    def test_rrf_fusion_strategy_round_trips(self, config_at_env_path) -> None:
        config_at_env_path("""
            retrieval:
              fusion_strategy: rrf
        """)
        cfg = load_config(env=config_at_env_path.env)
        assert cfg.fusion_strategy == "rrf"

    def test_custom_rrf_k_round_trips(self, config_at_env_path) -> None:
        config_at_env_path("""
            retrieval:
              rrf_k: 30
        """)
        cfg = load_config(env=config_at_env_path.env)
        assert cfg.rrf_k == 30


# ---------------------------------------------------------------------------
# load_collections — file loading + per-collection overrides observed through
# resolve_retrieval_config(collection=...)
# ---------------------------------------------------------------------------


@pytest.fixture
def kairix_config_with_collections(config_at_env_path):
    """Write a kairix.config.yaml with a ``collections.shared`` block including a
    per-collection ``retrieval:`` override on the ``archive`` entry. Yields a
    namespace with ``.env`` (mapping to pass to ``load_*(env=...)``) and
    ``.config_fn`` / ``.overrides_fn`` (callables to pass into
    ``resolve_retrieval_config(...)`` so it bypasses process-env reads).
    """
    from types import SimpleNamespace

    config_at_env_path("""
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
    """)
    env = config_at_env_path.env

    def _config_fn() -> RetrievalConfig:
        return load_config(env=env)

    def _overrides_fn() -> dict[str, dict]:
        cfg = load_collections(env=env)
        if not cfg:
            return {}
        return {c.name: c.retrieval_overrides for c in cfg.shared if c.retrieval_overrides}

    yield SimpleNamespace(env=env, config_fn=_config_fn, overrides_fn=_overrides_fn)


@pytest.mark.unit
def test_load_collections_returns_parsed_config_when_yaml_present(kairix_config_with_collections) -> None:
    cfg = load_collections(env=kairix_config_with_collections.env)
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
def test_load_collections_returns_none_when_yaml_unparseable(config_at_env_path) -> None:
    """When the configured path is unparseable YAML, ``load_collections`` returns None."""
    # Conflicting block-mapping keys make yaml.safe_load raise.
    config_at_env_path("collections:\n  shared:\n    - name: x\n  - bogus_top: bad\n")
    cfg = load_collections(env=config_at_env_path.env)
    assert cfg is None


@pytest.mark.unit
def test_resolve_retrieval_config_applies_per_collection_overrides(kairix_config_with_collections) -> None:
    """The ``archive`` collection has overrides → resolved config has the
    boosts.entity.factor override applied; the global default applies elsewhere.
    """
    archive_cfg = resolve_retrieval_config(
        collection="archive",
        config_fn=kairix_config_with_collections.config_fn,
        overrides_fn=kairix_config_with_collections.overrides_fn,
    )
    assert archive_cfg.entity.factor == pytest.approx(2.5)

    # 'home' has no overrides → resolved config equals the global config.
    home_cfg = resolve_retrieval_config(
        collection="home",
        config_fn=kairix_config_with_collections.config_fn,
        overrides_fn=kairix_config_with_collections.overrides_fn,
    )
    assert home_cfg.entity.factor == RetrievalConfig.defaults().entity.factor


@pytest.mark.unit
def test_resolve_retrieval_config_returns_global_when_no_overrides_for_collection(
    kairix_config_with_collections,
) -> None:
    """A collection name without per-collection overrides resolves to the global config."""
    cfg = resolve_retrieval_config(
        collection="home",
        config_fn=kairix_config_with_collections.config_fn,
        overrides_fn=kairix_config_with_collections.overrides_fn,
    )
    defaults = RetrievalConfig.defaults()
    assert cfg.entity.factor == defaults.entity.factor


@pytest.mark.unit
def test_resolve_retrieval_config_returns_global_when_no_config_file_present(no_config_path) -> None:
    cfg = resolve_retrieval_config(
        collection="archive",
        config_fn=lambda: load_config(**no_config_path),
        overrides_fn=lambda: {},
    )
    assert cfg.entity.factor == RetrievalConfig.defaults().entity.factor


# ---------------------------------------------------------------------------
# Per-collection retrieval merge — temporal & rerank sub-blocks driven through
# resolve_retrieval_config(collection=...). The merge logic is internal; its
# behaviour is observed via the resulting RetrievalConfig fields.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_temporal_boost_overrides_merge_with_base_temporal_subconfig(config_at_env_path) -> None:
    """A per-collection ``boosts.temporal.date_path_boost`` override merges with
    the base temporal sub-config (the global ``load_config(env=config_at_env_path.env)`` output).
    Specifically-overridden values take effect; untouched fields fall back to
    the base — not to hard parser defaults.
    """
    config_at_env_path("""
        collections:
          shared:
            - name: archive
              path: 06-Archive
              retrieval:
                boosts:
                  temporal:
                    date_path_boost:
                      factor: 5.0
                      recency_window_days: 14
    """)
    base = load_config(env=config_at_env_path.env)  # global config (no top-level retrieval: block in YAML → defaults)
    merged = resolve_retrieval_config(
        collection="archive",
        config_fn=config_at_env_path.config_fn,
        overrides_fn=config_at_env_path.overrides_fn,
    )
    # Specifically-overridden values are applied.
    assert merged.temporal.date_path_boost_factor == 5.0
    assert merged.temporal.date_path_recency_window_days == 14
    # Non-overridden values within the same sub-block fall back to base.
    assert merged.temporal.date_path_boost_enabled == base.temporal.date_path_boost_enabled
    # An entirely-untouched sub-block stays at base.
    assert merged.temporal.chunk_date_boost_enabled == base.temporal.chunk_date_boost_enabled
    assert merged.temporal.chunk_date_decay_halflife_days == base.temporal.chunk_date_decay_halflife_days
    # Other top-level fields untouched.
    assert merged.fusion_strategy == base.fusion_strategy


@pytest.mark.unit
def test_procedural_overrides_merge_with_base_procedural_subconfig(config_at_env_path) -> None:
    """A per-collection ``boosts.procedural`` override merges with the base
    procedural sub-config — overridden fields apply, untouched fields fall back.
    """
    config_at_env_path("""
        collections:
          shared:
            - name: archive
              path: 06-Archive
              retrieval:
                boosts:
                  procedural:
                    factor: 1.7
    """)
    base = load_config(env=config_at_env_path.env)
    merged = resolve_retrieval_config(
        collection="archive",
        config_fn=config_at_env_path.config_fn,
        overrides_fn=config_at_env_path.overrides_fn,
    )
    assert merged.procedural.factor == pytest.approx(1.7)
    # Non-overridden field falls back to base.
    assert merged.procedural.enabled == base.procedural.enabled


@pytest.mark.unit
def test_rerank_overrides_merge_with_base_rerank_subconfig(config_at_env_path) -> None:
    """A per-collection ``rerank`` override applies; non-overridden sub-fields fall back to base."""
    config_at_env_path("""
        collections:
          shared:
            - name: archive
              path: 06-Archive
              retrieval:
                rerank:
                  enabled: true
                  candidate_limit: 50
    """)
    base = load_config(env=config_at_env_path.env)
    merged = resolve_retrieval_config(
        collection="archive",
        config_fn=config_at_env_path.config_fn,
        overrides_fn=config_at_env_path.overrides_fn,
    )
    assert merged.rerank.enabled is True
    assert merged.rerank.candidate_limit == 50
    # Non-overridden rerank sub-field (model) keeps base default.
    assert merged.rerank.model == base.rerank.model


# ---------------------------------------------------------------------------
# resolve_retrieval_config — branches not driven by collection overrides
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_retrieval_config_returns_explicit_config_unchanged(no_config_path) -> None:
    """An explicit_config bypasses all loading and is returned as the same instance."""
    explicit = RetrievalConfig.defaults()
    result = resolve_retrieval_config(explicit_config=explicit)
    assert result is explicit


@pytest.mark.unit
def test_resolve_retrieval_config_with_no_collection_returns_global(no_config_path) -> None:
    """No collection target → returns the global config (no override merge)."""
    cfg = resolve_retrieval_config(
        config_fn=lambda: load_config(**no_config_path),
        overrides_fn=lambda: {},
    )
    assert cfg.entity.factor == RetrievalConfig.defaults().entity.factor


@pytest.mark.unit
def test_resolve_retrieval_config_uses_single_item_collections_list_as_target(
    kairix_config_with_collections,
) -> None:
    """``collections=["archive"]`` (length 1) is treated as ``collection="archive"`` →
    per-collection overrides apply.
    """
    cfg = resolve_retrieval_config(
        collections=["archive"],
        config_fn=kairix_config_with_collections.config_fn,
        overrides_fn=kairix_config_with_collections.overrides_fn,
    )
    assert cfg.entity.factor == pytest.approx(2.5)


# ---------------------------------------------------------------------------
# CollectionsConfig view methods — observed through load_collections(env=config_at_env_path.env) shape
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_default_collection_names_excludes_in_default_false(config_at_env_path) -> None:
    """``CollectionsConfig.default_collection_names`` filters out in_default=False entries."""
    config_at_env_path("""
        collections:
          shared:
            - name: home
              path: 00-Home
            - name: archive
              path: 06-Archive
              in_default: false
    """)
    cfg = load_collections(env=config_at_env_path.env)
    assert cfg is not None
    assert cfg.default_collection_names() == ["home"]
    # all_collection_names returns every entry regardless of in_default.
    assert set(cfg.all_collection_names()) == {"home", "archive"}


@pytest.mark.unit
def test_non_bool_in_default_value_makes_load_collections_return_none(config_at_env_path) -> None:
    """A quoted ``in_default: "false"`` (string, not bool) raises inside ``_coerce_bool``;
    ``load_collections`` catches it and returns None — the operator-facing signal that
    the file is malformed and defaults will be used.
    """
    config_at_env_path("""
        collections:
          shared:
            - name: home
              path: 00-Home
              in_default: "false"
    """)
    assert load_collections(env=config_at_env_path.env) is None
