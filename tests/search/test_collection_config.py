"""Tests for per-collection retrieval config resolution."""

from __future__ import annotations

import pytest

from kairix.core.search.config import RetrievalConfig
from kairix.core.search.config_loader import (
    ResolveConfigDeps,
    merge_retrieval_config,
    resolve_retrieval_config,
)

pytestmark = pytest.mark.unit


class TestMergeRetrievalConfig:
    @pytest.mark.unit
    def test_top_level_override(self) -> None:
        base = RetrievalConfig.defaults()
        merged = merge_retrieval_config(base, {"fusion_strategy": "rrf", "vec_limit": 30})
        assert merged.fusion_strategy == "rrf"
        assert merged.vec_limit == 30
        assert merged.bm25_limit == base.bm25_limit  # unchanged

    @pytest.mark.unit
    def test_nested_entity_override(self) -> None:
        base = RetrievalConfig.defaults()
        merged = merge_retrieval_config(base, {"boosts": {"entity": {"factor": 0.50}}})
        assert merged.entity.factor == pytest.approx(0.50)
        assert merged.entity.cap == base.entity.cap  # unchanged
        assert merged.entity.enabled == base.entity.enabled  # unchanged

    @pytest.mark.unit
    def test_nested_procedural_override(self) -> None:
        base = RetrievalConfig.defaults()
        merged = merge_retrieval_config(base, {"boosts": {"procedural": {"factor": 2.0}}})
        assert merged.procedural.factor == pytest.approx(2.0)
        assert merged.procedural.enabled == base.procedural.enabled

    @pytest.mark.unit
    def test_empty_override_returns_base(self) -> None:
        base = RetrievalConfig.defaults()
        merged = merge_retrieval_config(base, {})
        assert merged == base

    @pytest.mark.unit
    def test_full_override(self) -> None:
        base = RetrievalConfig.defaults()
        merged = merge_retrieval_config(
            base,
            {
                "fusion_strategy": "rrf",
                "rrf_k": 40,
                "bm25_limit": 10,
                "vec_limit": 5,
                "boosts": {
                    "entity": {"enabled": False},
                    "procedural": {"enabled": False},
                },
            },
        )
        assert merged.fusion_strategy == "rrf"
        assert merged.rrf_k == 40
        assert merged.bm25_limit == 10
        assert merged.vec_limit == 5
        assert merged.entity.enabled is False
        assert merged.procedural.enabled is False

    @pytest.mark.unit
    def test_rerank_intents_override(self) -> None:
        """Per-collection rerank_intents override (closes #74) — operators can
        narrow which intents trigger rerank for a specific collection. e.g.
        reference-library benchmarks show rerank helps conceptual but hurts
        multi_hop, so reflib's collection should override the global default
        of ('multi_hop', 'semantic') to ('conceptual',) only.
        """
        base = RetrievalConfig.defaults()
        # Default is ("multi_hop", "semantic") on RetrievalConfig.
        merged = merge_retrieval_config(base, {"rerank_intents": ["conceptual"]})
        assert merged.rerank_intents == ("conceptual",)
        # Other fields unchanged.
        assert merged.fusion_strategy == base.fusion_strategy

    @pytest.mark.unit
    def test_rerank_intents_empty_disables_per_intent_rerank(self) -> None:
        """An empty list disables rerank entirely for the collection — the
        rerank trigger requires intent-membership, so empty == always-off
        unless ``rerank.enabled`` is True (the global force-on lever).
        """
        base = RetrievalConfig.defaults()
        merged = merge_retrieval_config(base, {"rerank_intents": []})
        assert merged.rerank_intents == ()


class TestResolveRetrievalConfig:
    @pytest.mark.unit
    def test_explicit_config_wins(self) -> None:
        explicit = RetrievalConfig.minimal()
        result = resolve_retrieval_config(
            collection="reference-library",
            explicit_config=explicit,
        )
        assert result is explicit

    @pytest.mark.unit
    def test_reflib_uses_per_collection_overrides(self) -> None:
        """reference-library now reaches its baseline via per-collection retrieval overrides.

        The example yaml ships an explicit ``retrieval:`` block on the
        reference-library entry whose values match the historical
        ``REFLIB_RETRIEVAL_CONFIG`` baseline. This test simulates that yaml
        shape via the ``overrides_fn`` injection seam.
        """
        baseline_overrides = {
            "reference-library": {
                "fusion_strategy": "bm25_primary",
                "bm25_limit": 20,
                "vec_limit": 5,
                "boosts": {
                    "entity": {"enabled": True, "factor": 0.20, "cap": 2.0},
                    "procedural": {"enabled": True, "factor": 1.4},
                },
            }
        }
        result = resolve_retrieval_config(
            collections=["reference-library"],
            deps=ResolveConfigDeps(
                config_fn=RetrievalConfig.defaults,
                overrides_fn=lambda: baseline_overrides,
            ),
        )
        assert result.fusion_strategy == "bm25_primary"
        assert result.vec_limit == 5
        assert result.bm25_limit == 20
        assert result.entity.factor == pytest.approx(0.20)
        assert result.procedural.factor == pytest.approx(1.4)

    @pytest.mark.unit
    def test_reflib_without_override_uses_global(self) -> None:
        """When no per-collection block is set, reflib gets the global config like any other collection.

        The hardcoded ``if target == "reference-library":`` branch was deleted
        in v2026.5.4 — reflib's retrieval shape now lives in operator yaml
        (or in a shipped example), not in source.
        """
        global_cfg = RetrievalConfig.defaults()
        result = resolve_retrieval_config(
            collections=["reference-library"],
            deps=ResolveConfigDeps(
                config_fn=lambda: global_cfg,
                overrides_fn=lambda: {},
            ),
        )
        assert result is global_cfg

    @pytest.mark.unit
    def test_single_collection_with_yaml_config(self) -> None:
        result = resolve_retrieval_config(
            collections=["my-docs"],
            deps=ResolveConfigDeps(
                config_fn=RetrievalConfig.defaults,
                overrides_fn=lambda: {"my-docs": {"fusion_strategy": "rrf", "vec_limit": 30}},
            ),
        )
        assert result.fusion_strategy == "rrf"
        assert result.vec_limit == 30

    @pytest.mark.unit
    def test_multi_collection_uses_global(self) -> None:
        global_cfg = RetrievalConfig.defaults()
        result = resolve_retrieval_config(
            collections=["a", "b"],
            deps=ResolveConfigDeps(config_fn=lambda: global_cfg),
        )
        assert result is global_cfg

    @pytest.mark.unit
    def test_no_collection_uses_global(self) -> None:
        global_cfg = RetrievalConfig.defaults()
        result = resolve_retrieval_config(deps=ResolveConfigDeps(config_fn=lambda: global_cfg))
        assert result is global_cfg

    @pytest.mark.unit
    def test_unknown_collection_uses_global(self) -> None:
        global_cfg = RetrievalConfig.defaults()
        result = resolve_retrieval_config(
            collections=["unknown"],
            deps=ResolveConfigDeps(
                config_fn=lambda: global_cfg,
                overrides_fn=lambda: {},
            ),
        )
        assert result is global_cfg


class TestRefLibConfig:
    @pytest.mark.unit
    def test_baseline_values(self) -> None:
        from kairix.knowledge.reflib.retrieval_config import REFLIB_RETRIEVAL_CONFIG

        assert REFLIB_RETRIEVAL_CONFIG.fusion_strategy == "bm25_primary"
        assert REFLIB_RETRIEVAL_CONFIG.bm25_limit == 20
        assert REFLIB_RETRIEVAL_CONFIG.vec_limit == 5
        assert REFLIB_RETRIEVAL_CONFIG.entity.enabled is True
        assert REFLIB_RETRIEVAL_CONFIG.procedural.enabled is True


class TestParseCollectionsWithRetrieval:
    @pytest.mark.unit
    def test_retrieval_overrides_parsed(self) -> None:
        from kairix.core.search.config_loader import parse_collections

        data = {
            "collections": {
                "shared": [
                    {
                        "name": "docs",
                        "path": "docs",
                        "retrieval": {"fusion_strategy": "rrf", "vec_limit": 30},
                    },
                ],
            },
        }
        result = parse_collections(data)
        assert result is not None
        assert result.shared[0].retrieval_overrides == {
            "fusion_strategy": "rrf",
            "vec_limit": 30,
        }

    @pytest.mark.unit
    def test_no_retrieval_block_is_none(self) -> None:
        from kairix.core.search.config_loader import parse_collections

        data = {
            "collections": {
                "shared": [{"name": "docs", "path": "docs"}],
            },
        }
        result = parse_collections(data)
        assert result is not None
        assert result.shared[0].retrieval_overrides is None
