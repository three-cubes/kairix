"""Tests for RetrievalConfig dataclass and boost function integration."""

import pytest

from kairix.core.search.config import (
    EntityBoostConfig,
    ProceduralBoostConfig,
    RetrievalConfig,
    TemporalBoostConfig,
)
from kairix.core.search.rrf import (
    FusedResult,
    entity_boost_neo4j,
    procedural_boost,
    temporal_date_boost,
)


def _make_fused(path: str, rrf_score: float = 0.1) -> FusedResult:
    """Helper to create a FusedResult for testing."""
    r = FusedResult(path=path, collection="test", title="T", snippet="S")
    r.rrf_score = rrf_score
    r.boosted_score = rrf_score
    return r


# --- RetrievalConfig factory methods ---


@pytest.mark.unit
class TestRetrievalConfigFactories:
    @pytest.mark.unit
    def test_defaults_returns_sweep_optimised_config(self):
        """defaults() returns sweep-optimised config: RRF, boosts off, vec_limit=10."""
        cfg = RetrievalConfig.defaults()
        assert cfg.fusion_strategy == "rrf"
        assert cfg.rrf_k == 60
        assert cfg.vec_limit == 10
        assert cfg.entity.enabled is False
        assert cfg.procedural.enabled is False
        assert cfg.temporal.chunk_date_boost_enabled is True

    @pytest.mark.unit
    def test_minimal_disables_all_boosts(self):
        cfg = RetrievalConfig.minimal()
        assert cfg.entity.enabled is False
        assert cfg.procedural.enabled is False
        assert cfg.temporal.date_path_boost_enabled is False
        assert cfg.temporal.chunk_date_boost_enabled is False

    @pytest.mark.unit
    def test_for_daily_log_corpus_enables_date_path_boost(self):
        cfg = RetrievalConfig.for_daily_log_corpus()
        assert cfg.temporal.date_path_boost_enabled is True
        assert cfg.entity.enabled is True  # entity still on

    @pytest.mark.unit
    def test_for_technical_documentation_disables_entity(self):
        cfg = RetrievalConfig.for_technical_documentation()
        assert cfg.entity.enabled is False
        assert cfg.procedural.enabled is True
        assert r"/docs?/" in cfg.procedural.path_patterns

    @pytest.mark.unit
    def test_for_semantic_corpus_uses_rrf(self):
        cfg = RetrievalConfig.for_semantic_corpus()
        assert cfg.fusion_strategy == "rrf"
        assert cfg.entity.enabled is False

    @pytest.mark.unit
    def test_defaults_use_rrf(self):
        cfg = RetrievalConfig.defaults()
        assert cfg.fusion_strategy == "rrf"

    @pytest.mark.unit
    def test_minimal_uses_bm25_primary(self):
        cfg = RetrievalConfig.minimal()
        assert cfg.fusion_strategy == "bm25_primary"

    @pytest.mark.unit
    def test_fusion_strategy_configurable(self):
        cfg = RetrievalConfig(fusion_strategy="rrf")
        assert cfg.fusion_strategy == "rrf"

    @pytest.mark.unit
    def test_rrf_k_configurable(self):
        cfg = RetrievalConfig(fusion_strategy="rrf", rrf_k=20)
        assert cfg.rrf_k == 20

    @pytest.mark.unit
    def test_configs_are_frozen(self):
        cfg = RetrievalConfig.defaults()
        with pytest.raises((AttributeError, TypeError)):
            cfg.entity = EntityBoostConfig(enabled=False)  # type: ignore[misc]  # asserting frozen dataclass rejects mutation


# --- EntityBoostConfig integration ---


@pytest.mark.unit
class TestEntityBoostConfig:
    @pytest.mark.unit
    def test_disabled_returns_results_with_rrf_scores(self):
        results = [_make_fused("entity/builder.md", 0.5)]

        class _FakeNeo4j:
            available = True

            def cypher(self, q):
                return [{"vault_path": "entity/builder.md", "in_degree": 10}]

        cfg = EntityBoostConfig(enabled=False)
        out = entity_boost_neo4j(results, _FakeNeo4j(), config=cfg)
        assert out[0].boosted_score == pytest.approx(0.5)

    @pytest.mark.unit
    def test_enabled_boosts_known_entity(self):
        results = [_make_fused("entity/builder.md", 0.5)]

        class _FakeNeo4j:
            available = True

            def cypher(self, q):
                return [{"vault_path": "entity/builder.md", "in_degree": 10}]

        cfg = EntityBoostConfig(enabled=True)
        out = entity_boost_neo4j(results, _FakeNeo4j(), config=cfg)
        assert out[0].boosted_score > 0.5

    @pytest.mark.unit
    def test_custom_factor_affects_boost(self):
        results_default = [_make_fused("entity/builder.md", 0.5)]
        results_high = [_make_fused("entity/builder.md", 0.5)]

        class _FakeNeo4j:
            available = True

            def cypher(self, q):
                return [{"vault_path": "entity/builder.md", "in_degree": 10}]

        out_default = entity_boost_neo4j(results_default, _FakeNeo4j(), config=EntityBoostConfig(factor=0.20))
        out_high = entity_boost_neo4j(results_high, _FakeNeo4j(), config=EntityBoostConfig(factor=0.40))
        assert out_high[0].boosted_score > out_default[0].boosted_score


# --- ProceduralBoostConfig integration ---


@pytest.mark.unit
class TestProceduralBoostConfig:
    @pytest.mark.unit
    def test_disabled_returns_results_unchanged(self):
        results = [_make_fused("runbooks/setup.md", 0.3)]
        cfg = ProceduralBoostConfig(enabled=False)
        out = procedural_boost(results, config=cfg)
        assert out[0].boosted_score == pytest.approx(0.3)

    @pytest.mark.unit
    def test_enabled_boosts_matching_path(self):
        results = [_make_fused("runbooks/setup.md", 0.3)]
        cfg = ProceduralBoostConfig(enabled=True, factor=1.4)
        out = procedural_boost(results, config=cfg)
        assert out[0].boosted_score == pytest.approx(0.3 * 1.4)

    @pytest.mark.unit
    def test_custom_patterns_applied(self):
        results = [
            _make_fused("sop-onboarding.md", 0.3),
            _make_fused("notes/general.md", 0.3),
        ]
        cfg = ProceduralBoostConfig(
            enabled=True,
            factor=1.4,
            path_patterns=(r"(?:^|/)sop-",),
        )
        out = procedural_boost(results, config=cfg)
        sop = next(r for r in out if "sop" in r.path)
        other = next(r for r in out if "general" in r.path)
        assert sop.boosted_score > other.boosted_score

    @pytest.mark.unit
    def test_default_patterns_include_sop_guide_playbook(self):
        cfg = ProceduralBoostConfig()
        assert any("sop-" in p for p in cfg.path_patterns)
        assert any("guide-" in p for p in cfg.path_patterns)
        assert any("playbook-" in p for p in cfg.path_patterns)


# --- TemporalBoostConfig integration (date-path) ---


@pytest.mark.unit
class TestTemporalDatePathBoost:
    @pytest.mark.unit
    def test_disabled_by_default_returns_results_unchanged(self):
        results = [_make_fused("2026-03-22.md", 0.3)]
        cfg = TemporalBoostConfig(date_path_boost_enabled=False)
        out = temporal_date_boost(results, "what happened on 2026-03-22", config=cfg)
        assert out[0].boosted_score == pytest.approx(0.3)

    @pytest.mark.unit
    def test_enabled_boosts_date_matching_path(self):
        results = [
            _make_fused("2026-03-22.md", 0.3),
            _make_fused("notes/general.md", 0.3),
        ]
        cfg = TemporalBoostConfig(date_path_boost_enabled=True, date_path_boost_factor=1.35)
        out = temporal_date_boost(results, "what happened on 2026-03-22", config=cfg)
        date_doc = next(r for r in out if "2026-03-22" in r.path)
        other = next(r for r in out if "general" in r.path)
        assert date_doc.boosted_score > other.boosted_score


# --- Backward compatibility ---


@pytest.mark.unit
class TestBackwardCompatibility:
    @pytest.mark.unit
    def test_procedural_boost_no_config_uses_defaults(self):
        """Calling procedural_boost() with no config arg still works."""
        results = [_make_fused("runbooks/setup.md", 0.3)]
        out = procedural_boost(results)  # no config param
        assert out[0].boosted_score == pytest.approx(0.3 * 1.4)

    @pytest.mark.unit
    def test_temporal_date_boost_no_config_disabled_by_default(self):
        """Calling temporal_date_boost() with no config returns results unmodified."""
        results = [_make_fused("2026-03-22.md", 0.3)]
        out = temporal_date_boost(results, "what happened on 2026-03-22")
        # date_path_boost_enabled defaults to False, so no boost
        assert out[0].boosted_score == pytest.approx(0.3)
