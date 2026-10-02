# kairix/quality/scoring/

Pluggable scorer registry. Turns the per-query capture
(`QueryRunResult`) emitted by the unified benchmark mode dispatcher
(`kairix/quality/benchmark/modes/`, P3) into per-metric verdicts
(`ScorerResult`) and per-category aggregates.

This is the P2 layer of the unified benchmark initiative. Today it
ships the foundation types — `QueryRunResult`, `Scorer`, `ScorerResult`
— and grows scorer-by-scorer commit-by-commit. The final shape is:

```
kairix.quality.scoring.NDCGScorer       — NDCG@k with graded relevance
                       .HitAtKScorer    — Hit@K (binary, ≥1 relevance hit)
                       .MRRScorer       — Mean Reciprocal Rank
                       .LLMJudgeScorer  — LLM-as-judge 0.0-1.0
                       .LatencyScorer   — post-hoc p50/p95/p99 across runs
                       .ScorerRegistry  — name → instance map
                       .auto_select_scorers — suite-shape → scorer set
```

## Public surface

```python
from kairix.quality.scoring import (
    QueryRunResult,  # wire format, frozen dataclass
    ScorerResult,  # per-metric verdict
    Scorer,  # runtime-checkable Protocol
    NDCGScorer,
    HitAtKScorer,
    MRRScorer,
    LLMJudgeScorer,
    LatencyScorer,
    ScorerRegistry,
    auto_select_scorers,  # suite-shape → registry
    aggregate_by_category,
    aggregate_overall,
)
```

## Where each piece lives

- `types.py` — `QueryRunResult`, `ScorerResult`, `Scorer` Protocol,
  `LatencyPhase`. The hard contract between P3 dispatchers and P2
  scorers.
- `ndcg.py` — `NDCGScorer` (NDCG@k, graded relevance per
  `docs/evaluation/EVALUATION.md` §Metrics).
- `hit_at_k.py` — `HitAtKScorer` (binary Hit@K).
- `mrr.py` — `MRRScorer` (Mean Reciprocal Rank).
- `llm_judge.py` — `LLMJudgeScorer` (consumes a `LLMBackend`; extracts
  the judge prompt previously embedded in `suite_runner._judge`).
- `latency.py` — `LatencyScorer` (post-hoc percentile aggregation).
- `registry.py` — `ScorerRegistry`, `auto_select_scorers(suite, results)`.
- `aggregator.py` — per-category and overall aggregation (shared
  primitives the benchmark runner can call).
- `__init__.py` — exports the public symbols.

## Where each test lives

- `tests/quality/scoring/test_types.py` — `QueryRunResult` /
  `ScorerResult` dataclass + Protocol contract.
- `tests/quality/scoring/test_ndcg.py` — NDCG happy-path, empty-result,
  missing-gold, boundary, sabotage proofs.
- `tests/quality/scoring/test_hit_at_k.py` — Hit@K analogues.
- `tests/quality/scoring/test_mrr.py` — MRR analogues.
- `tests/quality/scoring/test_llm_judge.py` — judge with `FakeLLMBackend`
  fake injection.
- `tests/quality/scoring/test_latency.py` — percentile correctness +
  empty-set handling.
- `tests/quality/scoring/test_registry.py` — `ScorerRegistry` lookup
  semantics, `auto_select_scorers` selection logic.
- `tests/quality/scoring/test_aggregator.py` — per-category / overall
  aggregation parity with the existing benchmark runner.

## What does NOT belong here

- Mode dispatch (single-shot / concurrent / soak orchestration) —
  that's `kairix/quality/benchmark/modes/`, P3 territory.
- Suite YAML parsing — that's `kairix/quality/benchmark/suite.py`.
- CLI argument plumbing — that's `kairix/quality/benchmark/cli.py`
  (P5 consolidation).
- Production LLM-provider wire-ups — `LLMJudgeScorer` accepts an
  `LLMBackend` Protocol from `kairix.platform.llm.protocol`; the
  provider lookup lives in the provider plug-in layer.

## Architecture fitness functions touched

- **F1 / F5** — scorers are constructor-injected with their gold +
  backend dependencies; tests inject fakes via constructor seams, never
  monkeypatch.
- **F8** — every `test_*` here carries `pytest.mark.unit` via
  module-level `pytestmark`.
- **F22** — `kairix/quality/scoring/*.py` snake_case.
- **F23** — this README is the resolver for `kairix/quality/scoring/`.
- **F26** — `kairix/quality/scoring/` may import from
  `kairix.platform.llm.protocol` (the `LLMBackend` Protocol; a platform
  type) and `kairix.quality.eval.metrics` (the existing math
  primitives). It must NOT import `kairix.providers.*` or
  `kairix.transport.*`.
- **F29** — no perf-named files in this package. Latency aggregation
  here is post-hoc consumption of `QueryRunResult.latency_ms`; the
  measurement instrument lives at `kairix/quality/probe/`.
- **F30** — library-only module; no CLI subcommand, no MCP tool — F30
  does not apply.

See `docs/architecture/fitness-functions.md` for the canonical listing.
