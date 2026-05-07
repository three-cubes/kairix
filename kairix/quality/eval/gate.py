"""Quality gate — final stage of the onboarding flow (KFEAT-013, stage 5).

Takes a benchmark result and renders a go/hold verdict, with concrete
parameter-change recommendations for any category scoring below the floor.

Composition:

  benchmark run --suite <gold>      → result.json (category_scores)
  kairix.quality.eval.tune          → analyse_results, recommend
  kairix.quality.eval.gate          → run_gate (this module)

The gate is a pure function over already-computed benchmark scores. It
does not run the benchmark itself — that's the caller's job. This keeps
the gate trivially testable (pass in scores, get a verdict) without any
test-only kwargs in the production API.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kairix.quality.eval.tune import (
    CorpusHints,
    Recommendation,
    analyse_results,
    recommend,
)


@dataclass
class GateResult:
    """Outcome of evaluating a benchmark result against the quality floor.

    ``verdict`` is one of:
      - ``"pass"`` — every category is at or above ``floor``. Search is ready.
      - ``"hold"`` — at least one category is below ``floor``. Apply the
        recommendations and re-run the gate.
    """

    verdict: str
    weighted_total: float
    floor: float
    category_scores: dict[str, float]
    weak_categories: list[str] = field(default_factory=list)
    recommendations: list[Recommendation] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"

    def format(self) -> str:
        """Render the gate result as a human-readable string."""
        lines: list[str] = []
        emoji = "✅" if self.passed else "🛑"
        verdict_label = "PASS" if self.passed else "HOLD"
        lines.append(f"{emoji} Quality Gate: {verdict_label}")
        lines.append(f"   Weighted total: {self.weighted_total:.4f}")
        lines.append(f"   Floor:          {self.floor:.4f}")
        lines.append("")
        lines.append("Category scores:")
        for cat, score in sorted(self.category_scores.items()):
            mark = "✓" if score >= self.floor else "✗"
            lines.append(f"   {mark} {cat:<14} {score:.4f}")

        if self.weak_categories:
            lines.append("")
            lines.append(f"Weak categories ({len(self.weak_categories)}):")
            for cat in self.weak_categories:
                lines.append(f"   - {cat}")

        if self.recommendations:
            lines.append("")
            lines.append("Recommendations:")
            for rec in self.recommendations:
                lines.append(f"   • {rec.parameter}: {rec.action}")
                lines.append(f"     {rec.reason}")
                lines.append(f"     Expected impact: {rec.expected_impact}")

        if self.passed and not self.recommendations:
            lines.append("")
            lines.append("All categories at or above the quality floor — no changes recommended.")

        return "\n".join(lines)


def run_gate(
    scores: dict[str, float],
    *,
    weighted_total: float,
    hints: CorpusHints | None = None,
    floor: float = 0.50,
) -> GateResult:
    """Evaluate a benchmark result against the quality floor.

    Args:
        scores: per-category NDCG scores (e.g. ``{"recall": 0.82, ...}``).
        weighted_total: the suite's weighted-total NDCG, for reporting.
        hints: corpus characteristics that gate which recommendations apply.
            Defaults to no hints — recommendations stay generic.
        floor: minimum acceptable per-category score. Categories below this
            float trigger ``hold`` and produce recommendations.

    Returns:
        ``GateResult`` with the verdict, scores, and any recommendations.
    """
    analysis = analyse_results(scores, floor=floor)
    weak = list(analysis.weak_categories)
    recs = recommend(weak, hints or CorpusHints())

    verdict = "pass" if not weak else "hold"

    return GateResult(
        verdict=verdict,
        weighted_total=weighted_total,
        floor=floor,
        category_scores=dict(scores),
        weak_categories=weak,
        recommendations=recs,
    )
