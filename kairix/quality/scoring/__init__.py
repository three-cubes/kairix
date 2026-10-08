"""Pluggable scorer registry — turns captured ``QueryRunResult``s into per-metric scores.

P2 of the unified benchmark initiative. The benchmark runner (P3) executes
queries via mode dispatchers and emits one ``QueryRunResult`` per query.
This package converts those into per-scorer verdicts: NDCG, Hit@K, MRR,
LLM-judge, and Latency.

```python
from kairix.quality.scoring import (
    QueryRunResult,
    Scorer,
    ScorerResult,
    NDCGScorer,
    HitAtKScorer,
    MRRScorer,
    aggregate_by_category,
    aggregate_overall,
)
```

Subsequent commits add ``LLMJudgeScorer``, ``LatencyScorer``,
``ScorerRegistry`` + ``auto_select_scorers``. See ``README.md`` for the
architectural map. F26-clean: no provider, no transport imports anywhere
in this package.
"""

from kairix.quality.scoring.aggregator import (
    CategoryAggregate,
    OverallAggregate,
    aggregate_by_category,
    aggregate_overall,
)
from kairix.quality.scoring.hit_at_k import HitAtKScorer
from kairix.quality.scoring.latency import LatencyScorer
from kairix.quality.scoring.llm_judge import LLMJudgeScorer
from kairix.quality.scoring.mrr import MRRScorer
from kairix.quality.scoring.ndcg import NDCGScorer
from kairix.quality.scoring.registry import ScorerRegistry, auto_select_scorers
from kairix.quality.scoring.types import (
    JudgeFailedError,
    LatencyPhase,
    QueryRunResult,
    Scorer,
    ScorerResult,
)

__all__ = [
    "CategoryAggregate",
    "HitAtKScorer",
    "JudgeFailedError",
    "LLMJudgeScorer",
    "LatencyPhase",
    "LatencyScorer",
    "MRRScorer",
    "NDCGScorer",
    "OverallAggregate",
    "QueryRunResult",
    "Scorer",
    "ScorerRegistry",
    "ScorerResult",
    "aggregate_by_category",
    "aggregate_overall",
    "auto_select_scorers",
]
