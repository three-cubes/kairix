"""Contract probes for ``kairix.core.search.config``.

Every documented contract for the config module gets a probe here:

  * Each ``RetrievalConfig`` factory class method returns a config whose
    field values match its docstring (``defaults()``, ``minimal()``,
    ``for_daily_log_corpus()``, ``for_technical_documentation()``,
    ``for_semantic_corpus()``).
  * ``FUSION_STRATEGIES`` matches the literal docstring claim — exactly
    ``"bm25_primary"`` and ``"rrf"``.
  * ``REFLIB_RETRIEVAL_CONFIG`` field values match the docstring claims
    in ``config.py`` (the in-source baseline, not the re-export).
  * Frozen-dataclass invariants — ``RetrievalConfig`` and every embedded
    boost / rerank dataclass raises ``FrozenInstanceError`` on mutation.

Integration is intentionally skipped: every config in this module is pure
data with no validators, factories, or external I/O. The only place a
config is "wired" is ``SearchPipeline.config`` — a plain dataclass field
that accepts any ``RetrievalConfig`` and reads scalar attributes
(``bm25_limit``, ``vec_limit``, ``skip_vector``). That gives no
config-level integration contract beyond what the per-field probes
already cover.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from kairix.core.search.config import (
    FUSION_STRATEGIES,
    REFLIB_RETRIEVAL_CONFIG,
    EntityBoostConfig,
    ProceduralBoostConfig,
    RerankConfig,
    RetrievalConfig,
    TemporalBoostConfig,
)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


@pytest.mark.contract
class TestFusionStrategiesConstant:
    """``FUSION_STRATEGIES`` is the source of truth for valid fusion values.

    The module docstring documents exactly two strategies: ``"rrf"`` (default)
    and ``"bm25_primary"``. The constant must contain those two values and no
    others — adding a third silently would make the docstring lie.
    """

    @pytest.mark.contract
    def test_fusion_strategies_matches_docstring(self) -> None:
        assert set(FUSION_STRATEGIES) == {"bm25_primary", "rrf"}

    @pytest.mark.contract
    def test_fusion_strategies_has_exactly_two_entries(self) -> None:
        assert len(FUSION_STRATEGIES) == 2

    @pytest.mark.contract
    def test_fusion_strategies_is_immutable_tuple(self) -> None:
        assert isinstance(FUSION_STRATEGIES, tuple)

    @pytest.mark.contract
    def test_default_fusion_strategy_is_in_constant(self) -> None:
        # The dataclass default for fusion_strategy must itself be a member
        # of FUSION_STRATEGIES — otherwise the default would be invalid.
        assert RetrievalConfig().fusion_strategy in FUSION_STRATEGIES


# ---------------------------------------------------------------------------
# RetrievalConfig top-level defaults
# ---------------------------------------------------------------------------


@pytest.mark.contract
class TestRetrievalConfigDefaults:
    """Top-level field defaults documented in ``RetrievalConfig`` source.

    Covers fields read directly by ``SearchPipeline``: ``bm25_limit``,
    ``vec_limit``, ``skip_vector``, ``rrf_k``, ``rerank_intents``.
    """

    @pytest.mark.contract
    def test_default_fusion_strategy_is_bm25_primary(self) -> None:
        assert RetrievalConfig().fusion_strategy == "bm25_primary"

    @pytest.mark.contract
    def test_default_rrf_k_is_60(self) -> None:
        assert RetrievalConfig().rrf_k == 60

    @pytest.mark.contract
    def test_default_bm25_limit_is_20(self) -> None:
        assert RetrievalConfig().bm25_limit == 20

    @pytest.mark.contract
    def test_default_vec_limit_is_20(self) -> None:
        assert RetrievalConfig().vec_limit == 20

    @pytest.mark.contract
    def test_default_skip_vector_is_false(self) -> None:
        assert RetrievalConfig().skip_vector is False

    @pytest.mark.contract
    def test_default_rerank_intents_are_multi_hop_and_semantic(self) -> None:
        assert RetrievalConfig().rerank_intents == ("multi_hop", "semantic")


# ---------------------------------------------------------------------------
# Sub-config defaults (Entity / Procedural / Temporal / Rerank)
# ---------------------------------------------------------------------------


@pytest.mark.contract
class TestEntityBoostConfigDefaults:
    """Defaults documented as inline comments in ``EntityBoostConfig``."""

    @pytest.mark.contract
    def test_enabled_default_is_true(self) -> None:
        assert EntityBoostConfig().enabled is True

    @pytest.mark.contract
    def test_factor_default_is_0_20(self) -> None:
        assert EntityBoostConfig().factor == pytest.approx(0.20)

    @pytest.mark.contract
    def test_cap_default_is_2_0(self) -> None:
        assert EntityBoostConfig().cap == pytest.approx(2.0)


@pytest.mark.contract
class TestProceduralBoostConfigDefaults:
    """Defaults documented in ``ProceduralBoostConfig``."""

    @pytest.mark.contract
    def test_enabled_default_is_true(self) -> None:
        assert ProceduralBoostConfig().enabled is True

    @pytest.mark.contract
    def test_factor_default_is_1_4(self) -> None:
        assert ProceduralBoostConfig().factor == pytest.approx(1.4)

    @pytest.mark.contract
    def test_default_path_patterns_are_immutable_tuple(self) -> None:
        assert isinstance(ProceduralBoostConfig().path_patterns, tuple)

    @pytest.mark.contract
    def test_default_path_patterns_include_documented_kinds(self) -> None:
        # The default tuple is documented to cover the seven procedural
        # filename prefixes / directory shapes commonly used in vaults.
        patterns = ProceduralBoostConfig().path_patterns
        joined = "\n".join(patterns)
        for marker in (
            "how-to-",
            "runbook",
            "procedure",
            "sop-",
            "guide-",
            "playbook-",
        ):
            assert marker in joined, f"default patterns missing {marker!r}"


@pytest.mark.contract
class TestTemporalBoostConfigDefaults:
    """Defaults documented inline in ``TemporalBoostConfig``."""

    @pytest.mark.contract
    def test_date_path_boost_disabled_by_default(self) -> None:
        assert TemporalBoostConfig().date_path_boost_enabled is False

    @pytest.mark.contract
    def test_date_path_boost_factor_default_is_1_35(self) -> None:
        assert TemporalBoostConfig().date_path_boost_factor == pytest.approx(1.35)

    @pytest.mark.contract
    def test_date_path_recency_window_default_is_90_days(self) -> None:
        assert TemporalBoostConfig().date_path_recency_window_days == 90

    @pytest.mark.contract
    def test_chunk_date_boost_disabled_by_default(self) -> None:
        assert TemporalBoostConfig().chunk_date_boost_enabled is False

    @pytest.mark.contract
    def test_chunk_date_decay_halflife_default_is_30_days(self) -> None:
        assert TemporalBoostConfig().chunk_date_decay_halflife_days == 30

    @pytest.mark.contract
    def test_chunk_date_guard_explicit_only_default_is_true(self) -> None:
        # Documented guard: chunk_date boost only fires when the query has an
        # explicit temporal marker. Default must remain True to prevent
        # accidental recency bias on TEMPORAL-intent queries.
        assert TemporalBoostConfig().chunk_date_boost_guard_explicit_only is True


@pytest.mark.contract
class TestRerankConfigDefaults:
    """Defaults documented inline in ``RerankConfig``."""

    @pytest.mark.contract
    def test_enabled_default_is_false(self) -> None:
        assert RerankConfig().enabled is False

    @pytest.mark.contract
    def test_default_model_is_minilm_l_6_v2(self) -> None:
        assert RerankConfig().model == "cross-encoder/ms-marco-MiniLM-L-6-v2"

    @pytest.mark.contract
    def test_default_candidate_limit_is_20(self) -> None:
        assert RerankConfig().candidate_limit == 20


# ---------------------------------------------------------------------------
# Factory class method contracts — one test per documented method
# ---------------------------------------------------------------------------


@pytest.mark.contract
class TestRetrievalConfigDefaultsFactory:
    """``RetrievalConfig.defaults()`` — sweep-optimised production defaults.

    Docstring claims: RRF fusion, k=60, vec_limit=10, entity disabled,
    procedural disabled, chunk_date enabled.
    """

    @pytest.mark.contract
    def test_returns_retrieval_config_instance(self) -> None:
        assert isinstance(RetrievalConfig.defaults(), RetrievalConfig)

    @pytest.mark.contract
    def test_fusion_strategy_is_rrf(self) -> None:
        assert RetrievalConfig.defaults().fusion_strategy == "rrf"

    @pytest.mark.contract
    def test_rrf_k_is_60(self) -> None:
        assert RetrievalConfig.defaults().rrf_k == 60

    @pytest.mark.contract
    def test_vec_limit_is_10(self) -> None:
        assert RetrievalConfig.defaults().vec_limit == 10

    @pytest.mark.contract
    def test_entity_boost_disabled(self) -> None:
        assert RetrievalConfig.defaults().entity.enabled is False

    @pytest.mark.contract
    def test_procedural_boost_disabled(self) -> None:
        assert RetrievalConfig.defaults().procedural.enabled is False

    @pytest.mark.contract
    def test_chunk_date_boost_enabled(self) -> None:
        assert RetrievalConfig.defaults().temporal.chunk_date_boost_enabled is True


@pytest.mark.contract
class TestRetrievalConfigMinimalFactory:
    """``RetrievalConfig.minimal()`` — all boosts disabled, bm25_primary fusion."""

    @pytest.mark.contract
    def test_returns_retrieval_config_instance(self) -> None:
        assert isinstance(RetrievalConfig.minimal(), RetrievalConfig)

    @pytest.mark.contract
    def test_fusion_strategy_is_bm25_primary(self) -> None:
        assert RetrievalConfig.minimal().fusion_strategy == "bm25_primary"

    @pytest.mark.contract
    def test_entity_boost_disabled(self) -> None:
        assert RetrievalConfig.minimal().entity.enabled is False

    @pytest.mark.contract
    def test_procedural_boost_disabled(self) -> None:
        assert RetrievalConfig.minimal().procedural.enabled is False

    @pytest.mark.contract
    def test_date_path_boost_disabled(self) -> None:
        assert RetrievalConfig.minimal().temporal.date_path_boost_enabled is False

    @pytest.mark.contract
    def test_chunk_date_boost_disabled(self) -> None:
        assert RetrievalConfig.minimal().temporal.chunk_date_boost_enabled is False


@pytest.mark.contract
class TestRetrievalConfigForDailyLogCorpusFactory:
    """``for_daily_log_corpus()`` — date-path temporal boost enabled."""

    @pytest.mark.contract
    def test_returns_retrieval_config_instance(self) -> None:
        assert isinstance(RetrievalConfig.for_daily_log_corpus(), RetrievalConfig)

    @pytest.mark.contract
    def test_date_path_boost_enabled(self) -> None:
        cfg = RetrievalConfig.for_daily_log_corpus()
        assert cfg.temporal.date_path_boost_enabled is True

    @pytest.mark.contract
    def test_entity_boost_remains_default_enabled(self) -> None:
        # Docstring lists *only* date-path as the change vs defaults.
        # Entity must still default to enabled.
        cfg = RetrievalConfig.for_daily_log_corpus()
        assert cfg.entity.enabled is True

    @pytest.mark.contract
    def test_procedural_boost_remains_default_enabled(self) -> None:
        cfg = RetrievalConfig.for_daily_log_corpus()
        assert cfg.procedural.enabled is True

    @pytest.mark.contract
    def test_fusion_strategy_remains_bm25_primary_default(self) -> None:
        cfg = RetrievalConfig.for_daily_log_corpus()
        assert cfg.fusion_strategy == "bm25_primary"


@pytest.mark.contract
class TestRetrievalConfigForTechnicalDocumentationFactory:
    """``for_technical_documentation()`` — entity off, extended procedural patterns."""

    @pytest.mark.contract
    def test_returns_retrieval_config_instance(self) -> None:
        assert isinstance(RetrievalConfig.for_technical_documentation(), RetrievalConfig)

    @pytest.mark.contract
    def test_entity_boost_disabled(self) -> None:
        assert RetrievalConfig.for_technical_documentation().entity.enabled is False

    @pytest.mark.contract
    def test_procedural_boost_enabled(self) -> None:
        assert RetrievalConfig.for_technical_documentation().procedural.enabled is True

    @pytest.mark.contract
    def test_procedural_factor_is_1_5(self) -> None:
        # The technical-docs preset bumps the procedural factor from 1.4 to 1.5.
        assert RetrievalConfig.for_technical_documentation().procedural.factor == pytest.approx(1.5)

    @pytest.mark.contract
    def test_procedural_patterns_extended_with_tutorial_docs_reference(self) -> None:
        patterns = RetrievalConfig.for_technical_documentation().procedural.path_patterns
        assert r"(?:^|/)tutorial-" in patterns
        assert r"/docs?/" in patterns
        assert r"/reference/" in patterns

    @pytest.mark.contract
    def test_fusion_strategy_remains_bm25_primary(self) -> None:
        # Class docstring entry: "Entity off, extended procedural patterns,
        # bm25_primary fusion."
        cfg = RetrievalConfig.for_technical_documentation()
        assert cfg.fusion_strategy == "bm25_primary"


@pytest.mark.contract
class TestRetrievalConfigForSemanticCorpusFactory:
    """``for_semantic_corpus()`` — RRF fusion, entity disabled."""

    @pytest.mark.contract
    def test_returns_retrieval_config_instance(self) -> None:
        assert isinstance(RetrievalConfig.for_semantic_corpus(), RetrievalConfig)

    @pytest.mark.contract
    def test_fusion_strategy_is_rrf(self) -> None:
        assert RetrievalConfig.for_semantic_corpus().fusion_strategy == "rrf"

    @pytest.mark.contract
    def test_entity_boost_disabled(self) -> None:
        assert RetrievalConfig.for_semantic_corpus().entity.enabled is False

    @pytest.mark.contract
    def test_procedural_boost_remains_default_enabled(self) -> None:
        # Only entity is documented as turned off — procedural keeps its
        # default-enabled state.
        assert RetrievalConfig.for_semantic_corpus().procedural.enabled is True


# ---------------------------------------------------------------------------
# REFLIB_RETRIEVAL_CONFIG — module constant baseline
# ---------------------------------------------------------------------------


@pytest.mark.contract
class TestReflibRetrievalConfigBaseline:
    """``REFLIB_RETRIEVAL_CONFIG`` is the locked-in reflib search baseline.

    The source comment says "DO NOT MODIFY". Drift here means the published
    NDCG@10=0.679 baseline number no longer corresponds to the values
    actually used at runtime.
    """

    @pytest.mark.contract
    def test_is_a_retrieval_config(self) -> None:
        assert isinstance(REFLIB_RETRIEVAL_CONFIG, RetrievalConfig)

    @pytest.mark.contract
    def test_fusion_strategy_is_bm25_primary(self) -> None:
        assert REFLIB_RETRIEVAL_CONFIG.fusion_strategy == "bm25_primary"

    @pytest.mark.contract
    def test_bm25_limit_is_20(self) -> None:
        assert REFLIB_RETRIEVAL_CONFIG.bm25_limit == 20

    @pytest.mark.contract
    def test_vec_limit_is_5(self) -> None:
        assert REFLIB_RETRIEVAL_CONFIG.vec_limit == 5

    @pytest.mark.contract
    def test_entity_boost_enabled(self) -> None:
        assert REFLIB_RETRIEVAL_CONFIG.entity.enabled is True

    @pytest.mark.contract
    def test_entity_factor_is_0_20(self) -> None:
        assert REFLIB_RETRIEVAL_CONFIG.entity.factor == pytest.approx(0.20)

    @pytest.mark.contract
    def test_entity_cap_is_2_0(self) -> None:
        assert REFLIB_RETRIEVAL_CONFIG.entity.cap == pytest.approx(2.0)

    @pytest.mark.contract
    def test_procedural_boost_enabled(self) -> None:
        assert REFLIB_RETRIEVAL_CONFIG.procedural.enabled is True

    @pytest.mark.contract
    def test_procedural_factor_is_1_4(self) -> None:
        assert REFLIB_RETRIEVAL_CONFIG.procedural.factor == pytest.approx(1.4)

    @pytest.mark.contract
    def test_rerank_intents_is_empty(self) -> None:
        # Comment: "Reranking disabled — BM25-primary already ranks well for
        # this corpus". An empty tuple means no intent triggers a rerank.
        assert REFLIB_RETRIEVAL_CONFIG.rerank_intents == ()


# ---------------------------------------------------------------------------
# Frozen-dataclass invariants
# ---------------------------------------------------------------------------


@pytest.mark.contract
class TestFrozenDataclassInvariants:
    """Every config dataclass is declared ``frozen=True`` and must reject
    mutation with ``FrozenInstanceError`` (a subclass of ``AttributeError``).

    Mutability would let one search call leak config changes into another
    (configs are shared across pipeline invocations).
    """

    @pytest.mark.contract
    def test_retrieval_config_is_frozen(self) -> None:
        cfg = RetrievalConfig.defaults()
        with pytest.raises(FrozenInstanceError):
            cfg.fusion_strategy = "bm25_primary"  # type: ignore[misc]

    @pytest.mark.contract
    def test_retrieval_config_rejects_new_attribute(self) -> None:
        cfg = RetrievalConfig.defaults()
        with pytest.raises(FrozenInstanceError):
            cfg.brand_new_field = 123  # type: ignore[attr-defined]

    @pytest.mark.contract
    def test_entity_boost_config_is_frozen(self) -> None:
        cfg = EntityBoostConfig()
        with pytest.raises(FrozenInstanceError):
            cfg.enabled = False  # type: ignore[misc]

    @pytest.mark.contract
    def test_procedural_boost_config_is_frozen(self) -> None:
        cfg = ProceduralBoostConfig()
        with pytest.raises(FrozenInstanceError):
            cfg.factor = 9.9  # type: ignore[misc]

    @pytest.mark.contract
    def test_temporal_boost_config_is_frozen(self) -> None:
        cfg = TemporalBoostConfig()
        with pytest.raises(FrozenInstanceError):
            cfg.chunk_date_boost_enabled = True  # type: ignore[misc]

    @pytest.mark.contract
    def test_rerank_config_is_frozen(self) -> None:
        cfg = RerankConfig()
        with pytest.raises(FrozenInstanceError):
            cfg.enabled = True  # type: ignore[misc]

    @pytest.mark.contract
    def test_reflib_constant_is_frozen(self) -> None:
        # The shared module constant must not be mutable — otherwise a single
        # caller could permanently shift the baseline used by every other
        # caller in the process.
        with pytest.raises(FrozenInstanceError):
            REFLIB_RETRIEVAL_CONFIG.bm25_limit = 9999  # type: ignore[misc]
