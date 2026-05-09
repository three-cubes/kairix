"""
Tests for kairix.quality.eval.hybrid_sweep — hybrid pipeline calibration sweep.
"""

from pathlib import Path

import pytest

from kairix.quality.eval.hybrid_sweep import (
    CATEGORY_ALIASES,
    CATEGORY_WEIGHTS,
    HybridSweepConfig,
    HybridSweepReport,
    HybridSweepResult,
    build_default_configs,
    compute_hit_at_k,
    compute_mrr,
    compute_ndcg,
    sweep_config_to_retrieval_config,
    sweep_hybrid_params,
)
from kairix.quality.eval.metrics import relevance_for_path

# ---------------------------------------------------------------------------
# HybridSweepConfig
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHybridSweepConfig:
    @pytest.mark.unit
    def test_defaults(self) -> None:
        cfg = HybridSweepConfig(name="test", mode="hybrid")
        assert cfg.rrf_k == 60
        assert cfg.entity_enabled is True
        assert cfg.procedural_enabled is True
        assert cfg.bm25_limit == 20
        assert cfg.vec_limit == 10

    @pytest.mark.unit
    def test_bm25_only(self) -> None:
        cfg = HybridSweepConfig(name="bm25", mode="bm25_only")
        assert cfg.mode == "bm25_only"

    @pytest.mark.unit
    def test_frozen(self) -> None:
        cfg = HybridSweepConfig(name="test", mode="hybrid")
        with pytest.raises(AttributeError):
            cfg.rrf_k = 100  # type: ignore[misc]


# ---------------------------------------------------------------------------
# build_default_configs
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBuildDefaultConfigs:
    @pytest.mark.unit
    def test_returns_configs(self) -> None:
        configs = build_default_configs()
        assert len(configs) > 10

    @pytest.mark.unit
    def test_includes_bm25_only_baseline(self) -> None:
        configs = build_default_configs()
        names = [c.name for c in configs]
        assert "bm25-only" in names

    @pytest.mark.unit
    def test_includes_rrf_k_sweep(self) -> None:
        configs = build_default_configs()
        names = [c.name for c in configs]
        for k in [10, 20, 40, 60, 100]:
            assert f"hybrid-k{k}-minimal" in names

    @pytest.mark.unit
    def test_includes_entity_factor_sweep(self) -> None:
        configs = build_default_configs()
        names = [c.name for c in configs]
        assert any("entity-f" in n for n in names)

    @pytest.mark.unit
    def test_includes_procedural_factor_sweep(self) -> None:
        configs = build_default_configs()
        names = [c.name for c in configs]
        assert any("proc-f" in n for n in names)

    @pytest.mark.unit
    def test_includes_bm25_primary_configs(self) -> None:
        configs = build_default_configs()
        names = [c.name for c in configs]
        assert any("bm25primary" in n for n in names)

    @pytest.mark.unit
    def test_includes_tuned_combos(self) -> None:
        configs = build_default_configs()
        names = [c.name for c in configs]
        assert "hybrid-tuned-a" in names
        assert "hybrid-tuned-b" in names

    @pytest.mark.unit
    def test_all_configs_have_names(self) -> None:
        configs = build_default_configs()
        for cfg in configs:
            assert cfg.name
            assert cfg.mode in ("hybrid", "bm25_only", "bm25_primary")

    @pytest.mark.unit
    def test_no_duplicate_names(self) -> None:
        configs = build_default_configs()
        names = [c.name for c in configs]
        assert len(names) == len(set(names))


# ---------------------------------------------------------------------------
# Metrics: relevance_for_path (path-based gold matching)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMatchRelevance:
    @pytest.mark.unit
    def test_stem_only_match(self) -> None:
        gold = [{"title": "patterns", "relevance": 2}]
        assert relevance_for_path("vault/knowledge/patterns.md", gold) == 2

    @pytest.mark.unit
    def test_path_based_match(self) -> None:
        gold = [{"title": "engineering/adr-examples/readme", "relevance": 2}]
        assert relevance_for_path("reference-library/engineering/adr-examples/readme.md", gold) == 2

    @pytest.mark.unit
    def test_path_based_rejects_different_stem(self) -> None:
        gold = [{"title": "engineering/adr-examples/readme", "relevance": 2}]
        assert relevance_for_path("data-and-analysis/dbt-docs/readme.md", gold) == 0

    @pytest.mark.unit
    def test_no_match(self) -> None:
        gold = [{"title": "other-doc", "relevance": 1}]
        assert relevance_for_path("areas/kairix.md", gold) == 0

    @pytest.mark.unit
    def test_empty_gold(self) -> None:
        assert relevance_for_path("any/path.md", []) == 0

    @pytest.mark.unit
    def test_gold_paths_format(self) -> None:
        gold = [{"path": "areas/kairix.md", "relevance": 1}]
        assert relevance_for_path("vault/areas/kairix.md", gold) == 1


# ---------------------------------------------------------------------------
# Metrics: NDCG, Hit@k, MRR
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestComputeNdcg:
    @pytest.mark.unit
    def test_perfect_ranking(self) -> None:
        gold = [{"path": "a.md", "relevance": 2}, {"path": "b.md", "relevance": 1}]
        paths = ["a.md", "b.md"]
        ndcg = compute_ndcg(paths, gold, k=10)
        assert ndcg == pytest.approx(1.0)

    @pytest.mark.unit
    def test_reversed_ranking(self) -> None:
        gold = [{"path": "a.md", "relevance": 2}, {"path": "b.md", "relevance": 1}]
        paths = ["b.md", "a.md"]
        ndcg = compute_ndcg(paths, gold, k=10)
        assert 0.0 < ndcg < 1.0

    @pytest.mark.unit
    def test_no_relevant_docs(self) -> None:
        gold = [{"path": "a.md", "relevance": 2}]
        paths = ["x.md", "y.md"]
        ndcg = compute_ndcg(paths, gold, k=10)
        assert ndcg == pytest.approx(0.0)

    @pytest.mark.unit
    def test_empty_gold(self) -> None:
        assert compute_ndcg(["a.md"], [], k=10) == pytest.approx(0.0)

    @pytest.mark.unit
    def test_empty_retrieved(self) -> None:
        gold = [{"path": "a.md", "relevance": 2}]
        assert compute_ndcg([], gold, k=10) == pytest.approx(0.0)


@pytest.mark.unit
class TestComputeHitAtK:
    @pytest.mark.unit
    def test_hit_in_top_k(self) -> None:
        gold = [{"path": "a.md", "relevance": 1}]
        assert compute_hit_at_k(["x.md", "a.md", "y.md"], gold, k=5) is True

    @pytest.mark.unit
    def test_miss(self) -> None:
        gold = [{"path": "a.md", "relevance": 1}]
        assert compute_hit_at_k(["x.md", "y.md"], gold, k=5) is False

    @pytest.mark.unit
    def test_hit_at_boundary(self) -> None:
        gold = [{"path": "a.md", "relevance": 1}]
        paths = ["x.md", "y.md", "z.md", "w.md", "a.md"]
        assert compute_hit_at_k(paths, gold, k=5) is True

    @pytest.mark.unit
    def test_miss_beyond_k(self) -> None:
        gold = [{"path": "a.md", "relevance": 1}]
        paths = ["x.md", "y.md", "z.md", "w.md", "v.md", "a.md"]
        assert compute_hit_at_k(paths, gold, k=5) is False


@pytest.mark.unit
class TestComputeMrr:
    @pytest.mark.unit
    def test_first_position(self) -> None:
        gold = [{"path": "a.md", "relevance": 1}]
        assert compute_mrr(["a.md", "x.md"], gold) == pytest.approx(1.0)

    @pytest.mark.unit
    def test_second_position(self) -> None:
        gold = [{"path": "a.md", "relevance": 1}]
        assert compute_mrr(["x.md", "a.md"], gold) == pytest.approx(0.5)

    @pytest.mark.unit
    def test_no_relevant(self) -> None:
        gold = [{"path": "a.md", "relevance": 1}]
        assert compute_mrr(["x.md", "y.md"], gold) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Category weights
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_category_weights_sum_to_one() -> None:
    total = sum(CATEGORY_WEIGHTS.values())
    assert total == pytest.approx(1.0)


@pytest.mark.unit
def test_category_aliases_map_to_valid_weights() -> None:
    """All alias targets must exist in CATEGORY_WEIGHTS."""
    for alias, target in CATEGORY_ALIASES.items():
        assert target in CATEGORY_WEIGHTS, f"alias {alias!r}→{target!r} not in weights"


@pytest.mark.unit
def test_category_aliases_covers_suite_names() -> None:
    """semantic and keyword should be mapped."""
    assert "semantic" in CATEGORY_ALIASES
    assert "keyword" in CATEGORY_ALIASES


# ---------------------------------------------------------------------------
# HybridSweepResult and HybridSweepReport
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_sweep_result_defaults() -> None:
    cfg = HybridSweepConfig(name="test", mode="hybrid")
    r = HybridSweepResult(config=cfg)
    assert r.weighted_total == pytest.approx(0.0)
    assert r.n_vec_failed == 0
    assert r.category_scores == {}


@pytest.mark.unit
def test_sweep_report_defaults() -> None:
    report = HybridSweepReport()
    assert report.results == []
    assert report.best is None
    assert report.total_configs == 0


# ---------------------------------------------------------------------------
# sweep_config_to_retrieval_config
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_sweep_config_to_retrieval_config_hybrid() -> None:
    """Hybrid mode produces RRF fusion strategy."""
    cfg = HybridSweepConfig(name="test", mode="hybrid", rrf_k=40, entity_enabled=False)
    rc = sweep_config_to_retrieval_config(cfg)
    assert rc.fusion_strategy == "rrf"
    assert rc.rrf_k == 40
    assert rc.entity.enabled is False


@pytest.mark.unit
def test_sweep_config_to_retrieval_config_bm25_only() -> None:
    """BM25-only mode sets skip_vector and bm25_primary fusion."""
    cfg = HybridSweepConfig(name="test", mode="bm25_only")
    rc = sweep_config_to_retrieval_config(cfg)
    assert rc.skip_vector is True
    assert rc.fusion_strategy == "bm25_primary"


@pytest.mark.unit
def test_sweep_config_to_retrieval_config_bm25_primary() -> None:
    """BM25-primary mode sets bm25_primary fusion without skip_vector."""
    cfg = HybridSweepConfig(name="test", mode="bm25_primary")
    rc = sweep_config_to_retrieval_config(cfg)
    assert rc.fusion_strategy == "bm25_primary"
    assert rc.skip_vector is False


@pytest.mark.unit
def test_sweep_config_to_retrieval_config_preserves_boost_params() -> None:
    """Boost parameters are forwarded to RetrievalConfig."""
    cfg = HybridSweepConfig(
        name="test",
        mode="hybrid",
        entity_enabled=True,
        entity_factor=1.5,
        entity_cap=3,
        procedural_enabled=True,
        procedural_factor=2.0,
    )
    rc = sweep_config_to_retrieval_config(cfg)
    assert rc.entity.enabled is True
    assert rc.entity.factor == pytest.approx(1.5)
    assert rc.entity.cap == pytest.approx(3.0)
    assert rc.procedural.enabled is True
    assert rc.procedural.factor == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# sweep_hybrid_params
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_sweep_hybrid_params_with_mock(tmp_path: Path) -> None:
    """Sweep runs against a minimal suite and produces sorted results."""
    import yaml

    from kairix.quality.eval.retrieval import RetrievalResult
    from tests.fakes import FakeRetriever

    suite = {
        "meta": {"version": "1.0"},
        "cases": [
            {
                "query": "test query",
                "category": "recall",
                "score_method": "ndcg",
                "gold_titles": [{"title": "test-doc", "relevance": 2}],
            },
        ],
    }
    suite_path = tmp_path / "test-suite.yaml"
    with open(suite_path, "w") as f:
        yaml.dump(suite, f)

    canned = RetrievalResult(
        paths=["test-doc.md", "other.md"],
        meta={
            "bm25_count": 5,
            "vec_count": 3,
            "fused_count": 8,
            "vec_failed": False,
        },
    )
    retriever = FakeRetriever(results_by_query={"test query": canned})

    configs = [
        HybridSweepConfig(name="config-a", mode="hybrid", rrf_k=20),
        HybridSweepConfig(name="config-b", mode="hybrid", rrf_k=60),
    ]

    report = sweep_hybrid_params(suite_path, configs=configs, retriever=retriever)

    assert report.total_configs == 2
    assert len(report.results) == 2
    assert report.best is not None
    # Results sorted descending by weighted_total
    assert report.results[0].weighted_total >= report.results[1].weighted_total


@pytest.mark.unit
def test_sweep_hybrid_params_empty_cases(tmp_path: Path) -> None:
    """Sweep returns empty report when suite has no cases."""
    import yaml

    suite = {"meta": {"version": "1.0"}, "cases": []}
    suite_path = tmp_path / "empty-suite.yaml"
    with open(suite_path, "w") as f:
        yaml.dump(suite, f)

    report = sweep_hybrid_params(suite_path, configs=[])
    assert report.results == []
    assert report.best is None


@pytest.mark.unit
def test_sweep_hybrid_params_no_ndcg_cases(tmp_path: Path) -> None:
    """Sweep returns empty report when suite has no ndcg-scored cases."""
    import yaml

    suite = {
        "meta": {"version": "1.0"},
        "cases": [
            {"query": "q", "category": "recall", "score_method": "hit"},
        ],
    }
    suite_path = tmp_path / "no-ndcg-suite.yaml"
    with open(suite_path, "w") as f:
        yaml.dump(suite, f)

    report = sweep_hybrid_params(suite_path, configs=[])
    assert report.results == []
    assert report.best is None


@pytest.mark.unit
def test_sweep_hybrid_params_writes_csv(tmp_path: Path) -> None:
    """Sweep writes CSV when output_path is provided."""
    import yaml

    from kairix.quality.eval.retrieval import RetrievalResult
    from tests.fakes import FakeRetriever

    suite = {
        "meta": {"version": "1.0"},
        "cases": [
            {
                "query": "test query",
                "category": "recall",
                "score_method": "ndcg",
                "gold_titles": [{"title": "test-doc", "relevance": 2}],
            },
        ],
    }
    suite_path = tmp_path / "suite.yaml"
    with open(suite_path, "w") as f:
        yaml.dump(suite, f)

    csv_path = tmp_path / "results.csv"
    configs = [HybridSweepConfig(name="only", mode="hybrid")]

    canned = RetrievalResult(
        paths=["test-doc.md"],
        meta={"bm25_count": 1, "vec_count": 1, "fused_count": 2, "vec_failed": False},
    )
    retriever = FakeRetriever(results_by_query={"test query": canned})

    sweep_hybrid_params(suite_path, output_path=csv_path, configs=configs, retriever=retriever)

    assert csv_path.exists()
    lines = csv_path.read_text().splitlines()
    assert len(lines) == 2  # header + 1 data row
