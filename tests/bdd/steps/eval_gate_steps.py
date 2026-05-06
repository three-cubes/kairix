"""Step definitions for eval_gate.feature (KFEAT-013, stage 5).

Exercises the gate function directly via dependency-free composition: pass
in scores + hints, observe the GateResult. No fakes for production code,
no monkeypatch — the gate is a pure function over already-computed
benchmark scores. Corpus hints are constructed inline per scenario.

All step phrases are namespaced with "gate" tokens to avoid collisions
with the existing eval_tune_steps.py which uses similar Gherkin phrasing.
"""

from __future__ import annotations

import pytest
from pytest_bdd import given, parsers, then, when

from kairix.quality.eval.gate import GateResult, run_gate
from kairix.quality.eval.tune import CorpusHints

pytestmark = pytest.mark.bdd


@pytest.fixture
def gate_state() -> dict:
    return {"scores": {}, "hints": CorpusHints(), "result": None}


def _all_categories(value: float) -> dict[str, float]:
    return {
        "recall": value,
        "temporal": value,
        "entity": value,
        "conceptual": value,
        "multi_hop": value,
        "procedural": value,
    }


# ---------------------------------------------------------------------------
# Givens — score setup combined with corpus-hint context
# ---------------------------------------------------------------------------


@given(parsers.parse("a benchmark result with all categories at {value:f}"))
def all_categories_at(gate_state: dict, value: float) -> None:
    gate_state["scores"] = _all_categories(value)


@given(
    parsers.parse(
        "a benchmark result where temporal is {temporal:f} with date-named corpus and others are {others:f}"
    )
)
def temporal_low_with_date_corpus(gate_state: dict, temporal: float, others: float) -> None:
    gate_state["scores"] = _all_categories(others) | {"temporal": temporal}
    gate_state["hints"] = CorpusHints(has_date_files=True)


@given(
    parsers.parse(
        "a benchmark result where temporal is {temporal:f} with no date-named corpus and others are {others:f}"
    )
)
def temporal_low_no_date_corpus(gate_state: dict, temporal: float, others: float) -> None:
    gate_state["scores"] = _all_categories(others) | {"temporal": temporal}
    gate_state["hints"] = CorpusHints(has_date_files=False)


@given(
    parsers.parse(
        "a benchmark result where temporal is {temporal:f} and conceptual is {conceptual:f} "
        "with date-named corpus and others are {others:f}"
    )
)
def temporal_and_conceptual_low(gate_state: dict, temporal: float, conceptual: float, others: float) -> None:
    gate_state["scores"] = _all_categories(others) | {
        "temporal": temporal,
        "conceptual": conceptual,
    }
    gate_state["hints"] = CorpusHints(has_date_files=True)


@given(parsers.parse("a benchmark result where every category is exactly {value:f}"))
def every_category_at(gate_state: dict, value: float) -> None:
    gate_state["scores"] = _all_categories(value)


@given(
    parsers.parse(
        "a benchmark result where recall is {recall:f} and procedural is {procedural:f} with procedural-doc corpus"
    )
)
def recall_high_procedural_low(gate_state: dict, recall: float, procedural: float) -> None:
    gate_state["scores"] = _all_categories(0.80) | {
        "recall": recall,
        "procedural": procedural,
    }
    gate_state["hints"] = CorpusHints(has_procedural_docs=True)


# ---------------------------------------------------------------------------
# When — invoke the gate
# ---------------------------------------------------------------------------


@when(parsers.parse("I run the quality gate with floor {floor:f}"))
def run_quality_gate(gate_state: dict, floor: float) -> None:
    weighted = sum(gate_state["scores"].values()) / max(len(gate_state["scores"]), 1)
    gate_state["result"] = run_gate(
        gate_state["scores"],
        weighted_total=weighted,
        hints=gate_state["hints"],
        floor=floor,
    )


# ---------------------------------------------------------------------------
# Thens — assert the GateResult shape + content (gate-prefixed to avoid
# collision with eval_tune_steps Gherkin)
# ---------------------------------------------------------------------------


@then(parsers.parse('the verdict is "{verdict}"'))
def verdict_is(gate_state: dict, verdict: str) -> None:
    result: GateResult = gate_state["result"]
    assert result.verdict == verdict, f"expected {verdict}, got {result.verdict}"


@then("no weak categories are reported")
def no_weak(gate_state: dict) -> None:
    result: GateResult = gate_state["result"]
    assert result.weak_categories == [], f"expected no weak categories, got {result.weak_categories}"


@then("no gate recommendations are produced")
def no_recommendations(gate_state: dict) -> None:
    result: GateResult = gate_state["result"]
    assert result.recommendations == [], f"expected no recommendations, got {result.recommendations}"


@then(parsers.parse('"{cat}" is reported as a weak gate category'))
def category_reported_weak(gate_state: dict, cat: str) -> None:
    result: GateResult = gate_state["result"]
    assert cat in result.weak_categories, f"expected {cat} in {result.weak_categories}"


@then(parsers.parse('at least one gate recommendation targets "{cat}"'))
def recommendation_targets(gate_state: dict, cat: str) -> None:
    result: GateResult = gate_state["result"]
    matches = [r for r in result.recommendations if r.parameter == cat]
    assert matches, f"no recommendation for {cat}: {[r.parameter for r in result.recommendations]}"


@then(parsers.parse('both "{a}" and "{b}" are weak gate categories'))
def both_weak(gate_state: dict, a: str, b: str) -> None:
    result: GateResult = gate_state["result"]
    assert a in result.weak_categories and b in result.weak_categories, (
        f"expected both {a} and {b} weak, got {result.weak_categories}"
    )


@then("gate recommendations exist for both")
def recommendations_for_both(gate_state: dict) -> None:
    result: GateResult = gate_state["result"]
    parameters = {r.parameter for r in result.recommendations}
    assert len(parameters) >= 2, f"expected ≥2 distinct recommendation targets, got {parameters}"


@then(parsers.parse('"{cat}" is a weak gate category'))
def cat_is_weak(gate_state: dict, cat: str) -> None:
    result: GateResult = gate_state["result"]
    assert cat in result.weak_categories


@then(parsers.parse('no gate recommendation targets "{cat}" with date_path_boost'))
def no_date_boost_recommendation(gate_state: dict, cat: str) -> None:
    result: GateResult = gate_state["result"]
    matches = [r for r in result.recommendations if r.parameter == cat and "date_path_boost" in r.action]
    assert not matches, f"unexpected date_path_boost recommendation for {cat}: {matches}"


@then("the gate output contains the verdict label")
def output_contains_verdict(gate_state: dict) -> None:
    result: GateResult = gate_state["result"]
    formatted = result.format()
    label = "PASS" if result.passed else "HOLD"
    assert label in formatted, f"verdict {label} not in formatted output"


@then("the gate output contains every category score")
def output_contains_scores(gate_state: dict) -> None:
    result: GateResult = gate_state["result"]
    formatted = result.format()
    for cat in result.category_scores:
        assert cat in formatted, f"category {cat} missing from output"


@then(parsers.parse("the gate output contains the recommendation for {cat}"))
def output_contains_recommendation(gate_state: dict, cat: str) -> None:
    result: GateResult = gate_state["result"]
    formatted = result.format()
    matches = [r for r in result.recommendations if r.parameter == cat]
    assert matches, f"no recommendation for {cat} to verify in output"
    assert f"{cat}:" in formatted or f"{cat} " in formatted, (
        f"recommendation for {cat} not visible in formatted output"
    )
