"""Shared types for the pluggable scorer registry.

`QueryRunResult` is the mode-agnostic, dataclass-frozen capture of one
query's run output. Mode dispatchers (P3 — `kairix/quality/benchmark/modes/`)
produce these; scorers (`kairix/quality/scoring/{ndcg,hit_at_k,mrr,
llm_judge,latency}.py`) consume them.

`ScorerResult` is the scorer-side return shape — a per-query (or per-suite)
named-metric envelope with an optional `details` blob for diagnostics.

`Scorer` is the structural Protocol every concrete scorer satisfies. It is
runtime-checkable so contract tests can `isinstance(scorer, Scorer)` against
fakes and concrete implementations alike.

F26-clean: this module imports only from `typing` / `dataclasses` /
`collections.abc` / stdlib, plus the stdlib-only :mod:`kairix.quality.redaction`
formatter — no provider, no transport, no benchmark/runner dependency.
Other layers depend on us.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from kairix.quality.redaction import describe_exception


class JudgeFailedError(RuntimeError):
    """The LLM judge could not produce a score.

    Raised by every LLM judge — the benchmark relevance judge
    (:func:`kairix.quality.benchmark.runner.llm_judge`), the conversation
    suite judge (``SuiteRunner._judge``) and :class:`LLMJudgeScorer` — when
    the backend fails (auth, timeout, unconfigured provider, empty reply) or
    replies with something that is not a finite number. A failed judgement
    is NOT a verdict: callers must record the case as unscored and exclude
    it from aggregates instead of counting it as 0.0.

    Attributes:
        reason: Stable machine-readable failure class —
                :data:`JUDGE_FAILURE_BACKEND_ERROR` or
                :data:`JUDGE_FAILURE_UNPARSEABLE`.
        detail: Human-readable detail — the backend exception's class name
                only (never its message), or the length of the unparseable
                reply (never the reply text).
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"llm judge failed ({reason}): {detail}")
        self.reason = reason
        self.detail = detail

    @classmethod
    def from_backend_exception(cls, exc: BaseException) -> JudgeFailedError:
        """``backend_error`` carrying only the exception's class name.

        The exception *message* is never copied: provider errors can carry API
        keys, auth headers, request payloads or retrieved content, and
        ``detail`` is persisted into benchmark / eval JSON. The class name is
        code-defined and stays machine-readable (``backend raised TimeoutError``).
        """
        return cls(JUDGE_FAILURE_BACKEND_ERROR, f"backend {describe_exception(exc)}")


JUDGE_FAILURE_BACKEND_ERROR = "backend_error"
JUDGE_FAILURE_UNPARSEABLE = "unparseable_response"


LatencyPhase = Literal["cold", "warm", "load"]
"""Latency capture phase.

* ``cold`` — first call against an unwarmed pipeline (cold-cache, model-load).
* ``warm`` — steady-state call after warm-up.
* ``load`` — call inside a sustained-concurrency or burst run.

Different scorers gate against different phase mixes; the field is
declarative so the same scorer can be reused across modes without
recompilation.
"""


@dataclass(frozen=True)
class QueryRunResult:
    """One query's captured run output, mode-agnostic.

    Produced by mode dispatchers (P3 — `single_shot.py`, `concurrent.py`,
    `soak.py`); consumed by scorers (P2 — this package).

    Fields are deliberately minimal: each scorer reads only the slice it
    needs. ``ranked_doc_ids`` + ``ranked_doc_titles`` drive IR-style
    metrics (NDCG / Hit / MRR). ``synthesised_answer`` drives the
    LLM-judge scorer. ``latency_ms`` drives latency aggregation.

    The error path is reified — when a run fails, scorers see a sentinel
    ``QueryRunResult`` with ``error`` populated; the scorer's contract is
    to return a zero score (or skip) rather than re-raising.
    """

    query_id: str
    """Stable case identifier from the benchmark suite (BenchmarkCase.id)."""

    category: str
    """Query category (recall|temporal|entity|conceptual|multi_hop|procedural|classification)."""

    query_text: str
    """The query string that was actually executed."""

    # Retrieval outputs --------------------------------------------------
    ranked_doc_ids: tuple[str, ...] = ()
    """Ordered tuple of document identifiers, top result first.

    Empty when retrieval returned nothing (or when the mode is
    synthesis-only). Scorers that depend on ranking treat empty as
    "no signal" and return 0.0.
    """

    ranked_doc_titles: tuple[str, ...] = ()
    """Parallel tuple of document titles for gold-title matching.

    MUST be the same length as ``ranked_doc_ids`` when both are
    populated. Some retrieval backends only emit IDs; others only emit
    titles. Scorers tolerate either side being empty as long as at least
    one is populated.
    """

    # Synthesis outputs --------------------------------------------------
    synthesised_answer: str | None = None
    """LLM-generated answer text, or None when synthesis was skipped.

    Search-only modes leave this None; LLM-judge scorers treat None as
    "no answer to score" and return 0.0.
    """

    # Performance outputs -----------------------------------------------
    latency_ms: float = 0.0
    """Wall-clock latency in milliseconds — cold, warm, or load phase per ``latency_phase``."""

    latency_phase: LatencyPhase = "warm"
    """Capture phase — see :data:`LatencyPhase`."""

    # Error path --------------------------------------------------------
    error: str | None = None
    """Populated when the run failed; scorers handle gracefully.

    When non-None, scorers return ``ScorerResult(score=0.0, ...)`` with
    the error surfaced in ``details["error"]`` rather than raising.
    """


@dataclass(frozen=True)
class ScorerResult:
    """One scorer's verdict on one ``QueryRunResult`` (or aggregated set).

    ``score`` is the headline number every scorer must emit (NDCG@10,
    Hit@5, MRR@10, judge 0.0-1.0, p95 milliseconds, etc.). ``metric_name``
    is the registry key so a downstream aggregator can identify the
    column ("ndcg_at_10", "hit_at_5", "p95_ms"). ``details`` carries
    scorer-specific diagnostics (per-rank relevances for NDCG, judge
    rationale, latency percentile sample, etc.) — opaque to the rest of
    the system, useful for operators inspecting a regression.
    """

    metric_name: str
    """Stable identifier for the metric column (e.g. ``"ndcg_at_10"``)."""

    score: float
    """The numeric verdict; convention is "higher is better" except for latency."""

    details: dict[str, Any] = field(default_factory=dict)
    """Scorer-specific diagnostic blob — opaque to consumers."""


@runtime_checkable
class Scorer(Protocol):
    """Structural Protocol every concrete scorer satisfies.

    Two flavours of scorer exist:

    * Per-query scorers (NDCG, Hit@K, MRR, LLMJudge) — score one
      ``QueryRunResult`` against the gold reference baked into the
      scorer instance (constructor takes ``gold_titles`` /
      ``expected_answer``).
    * Aggregate scorers (Latency) — score across a *sequence* of
      ``QueryRunResult``s post-hoc (p50/p95/p99 only mean something
      across many runs).

    The Protocol surface is intentionally minimal — ``name`` for the
    registry, ``score`` for the verdict. Each concrete class adds its
    own constructor knobs (k, gold, threshold).
    """

    @property
    def name(self) -> str:
        """Stable registry name for the scorer (e.g. ``"ndcg"``)."""
        ...

    def score(self, run: QueryRunResult | Sequence[QueryRunResult], /) -> ScorerResult:
        """Score the run(s) and return the verdict — Protocol stub."""
