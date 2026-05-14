"""Step definitions for eval_tune.feature."""

from pytest_bdd import given, parsers, then, when

from kairix.quality.eval.tune import CorpusHints, analyse_results, recommend

# Module-level state (simple, test-scoped)
_state: dict = {}

# Default categories used when building scores dicts
_ALL_CATEGORIES = ["temporal", "procedural", "entity", "conceptual", "recall"]


@given(parsers.re(r"benchmark scores with (?P<category>\w+) at (?P<weak>[0-9.]+) and all others at (?P<rest>[0-9.]+)"))
def scores_with_one_weak(category: str, weak: str, rest: str) -> None:
    """Build a scores dict with one weak category and the rest at a baseline."""
    scores = {cat: float(rest) for cat in _ALL_CATEGORIES}
    scores[category] = float(weak)
    _state["scores"] = scores


@given(parsers.re(r"benchmark scores with all categories at (?P<value>[0-9.]+)"))
def scores_all_equal(value: str) -> None:
    """Build a scores dict with every category at the same value."""
    _state["scores"] = {cat: float(value) for cat in _ALL_CATEGORIES}


@given("the corpus has date-named files")
def corpus_has_date_files() -> None:
    hints = _state.get("hints", CorpusHints())
    hints.has_date_files = True
    _state["hints"] = hints


@given("the corpus has procedural documents")
def corpus_has_procedural_docs() -> None:
    hints = _state.get("hints", CorpusHints())
    hints.has_procedural_docs = True
    _state["hints"] = hints


@when("the operator requests tuning recommendations")
def request_recommendations() -> None:
    analysis = analyse_results(_state["scores"])
    _state["analysis"] = analysis
    hints = _state.get("hints", CorpusHints())
    _state["recs"] = recommend(analysis.weak_categories, hints)


@then(parsers.parse('a recommendation for parameter "{param}" is returned'))
def check_recommendation_parameter(param: str) -> None:
    recs = _state["recs"]
    params = [r.parameter for r in recs]
    assert param in params, f"Expected parameter {param!r} in {params}"


@then(parsers.parse('the recommendation action mentions "{text}"'))
def check_recommendation_action(text: str) -> None:
    recs = _state["recs"]
    found = any(text in r.action for r in recs)
    assert found, f"Expected action containing {text!r}, got {[r.action for r in recs]}"


@then("no recommendations are returned")
def check_no_recommendations() -> None:
    recs = _state["recs"]
    assert len(recs) == 0, f"Expected no recommendations, got {recs}"
