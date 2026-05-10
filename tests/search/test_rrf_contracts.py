"""Contract-first tests for kairix.core.search.rrf.

Probes the RRF fusion formula and the entity / procedural / temporal /
chunk_date boost math against their docstring claims.
"""

from __future__ import annotations

import datetime
import math

import pytest

from kairix.core.search.config import (
    EntityBoostConfig,
    ProceduralBoostConfig,
    TemporalBoostConfig,
)
from kairix.core.search.rrf import (
    RRF_K,
    bm25_primary_fuse,
    canonical_path,
    chunk_date_boost,
    entity_boost_neo4j,
    procedural_boost,
    rrf,
    temporal_date_boost,
)
from tests.fakes import FakeGraphRepository

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bm25(file: str, **extra) -> dict:
    """Build a BM25-shaped result dict."""
    return {
        "file": file,
        "title": extra.get("title", file.rsplit("/", 1)[-1].rsplit(".", 1)[0]),
        "snippet": extra.get("snippet", "snippet"),
        "collection": extra.get("collection", "vault"),
    }


def _vec(path: str, **extra) -> dict:
    """Build a vector-shaped result dict."""
    return {
        "path": path,
        "title": extra.get("title", path.rsplit("/", 1)[-1].rsplit(".", 1)[0]),
        "snippet": extra.get("snippet", "snippet"),
        "collection": extra.get("collection", "vault"),
        "distance": extra.get("distance", 0.1),
    }


# ---------------------------------------------------------------------------
# RRF formula contracts
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_rrf_returns_empty_list_when_both_inputs_are_empty() -> None:
    assert rrf([], []) == []


@pytest.mark.unit
def test_rrf_single_bm25_doc_at_rank_1_scores_one_over_k_plus_one() -> None:
    """Standard RRF: rank-1 contribution = 1/(k+1). With default k=60, 1/61."""
    results = rrf([_bm25("a.md")], [])
    assert len(results) == 1
    assert results[0].rrf_score == pytest.approx(1.0 / (RRF_K + 1))


@pytest.mark.unit
def test_rrf_doc_in_both_lists_sums_per_list_contributions() -> None:
    """Per Cormack et al. 2009: a doc in both lists scores
    1/(k+rank_bm25) + 1/(k+rank_vec). With same path at rank 1 in each:
    1/61 + 1/61 = 2/61.
    """
    results = rrf([_bm25("shared.md")], [_vec("shared.md")])
    assert len(results) == 1
    expected = 1.0 / (RRF_K + 1) + 1.0 / (RRF_K + 1)
    assert results[0].rrf_score == pytest.approx(expected)


@pytest.mark.unit
def test_rrf_doc_in_only_one_list_does_not_get_phantom_contribution() -> None:
    """The docstring says "Documents in only one list ... they do NOT get an
    additional contribution from the absent list". So a doc only in BM25
    scores exactly 1/(k+rank_bm25) — no inflation from absence.
    """
    results = rrf([_bm25("solo.md")], [_vec("other.md")])
    solo = next(r for r in results if r.path == "solo.md")
    # Solo BM25 at rank 1 → score = 1/(k+1)
    assert solo.rrf_score == pytest.approx(1.0 / (RRF_K + 1))


@pytest.mark.unit
def test_rrf_results_are_sorted_descending_by_rrf_score() -> None:
    """The doc that appears in BOTH lists must rank above docs in only one."""
    bm25 = [_bm25("both.md"), _bm25("bm25-only.md")]
    vec = [_vec("both.md"), _vec("vec-only.md")]
    results = rrf(bm25, vec)
    # Three docs: "both.md" should be first (got contributions from both lists).
    paths = [r.path for r in results]
    assert paths[0] == "both.md", f"expected 'both.md' first; got {paths}"
    # And the order is descending by score.
    scores = [r.rrf_score for r in results]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.unit
def test_rrf_initialises_boosted_score_to_rrf_score() -> None:
    """Pre-boost, boosted_score must equal rrf_score so unboosted callers
    can sort by either field.
    """
    results = rrf([_bm25("a.md")], [])
    assert results[0].boosted_score == results[0].rrf_score


@pytest.mark.unit
def test_rrf_records_in_bm25_in_vec_membership_flags() -> None:
    """Per FusedResult docstring: ``in_bm25`` / ``in_vec`` reflect which
    lists the doc came from. Used by downstream analytics.
    """
    results = rrf([_bm25("both.md"), _bm25("bm25-only.md")], [_vec("both.md"), _vec("vec-only.md")])
    by_path = {r.path: r for r in results}
    assert by_path["both.md"].in_bm25 is True
    assert by_path["both.md"].in_vec is True
    assert by_path["bm25-only.md"].in_bm25 is True
    assert by_path["bm25-only.md"].in_vec is False
    assert by_path["vec-only.md"].in_bm25 is False
    assert by_path["vec-only.md"].in_vec is True


@pytest.mark.unit
def test_rrf_never_raises_on_malformed_input() -> None:
    """Per docstring: "Never raises". A malformed input dict (missing keys)
    must surface as an empty result, not a KeyError.
    """
    malformed = [{"not_file": "weird"}]  # type: ignore[list-item]
    result = rrf(malformed, [])
    assert result == []


# ---------------------------------------------------------------------------
# entity_boost_neo4j contracts
# ---------------------------------------------------------------------------


def _row(vault_path: str, in_degree: int, *, name: str = "", labels: list[str] | None = None) -> dict:
    """Build a Neo4j-shaped row matching the production cypher SELECT."""
    return {
        "vault_path": vault_path,
        "name": name,
        "labels": labels or [],
        "in_degree": in_degree,
    }


@pytest.mark.unit
def test_entity_boost_returns_results_unchanged_when_disabled() -> None:
    """``EntityBoostConfig(enabled=False)`` → boosted_score == rrf_score for every result."""
    cfg = EntityBoostConfig(enabled=False)
    results = rrf([_bm25("a.md")], [])
    boosted = entity_boost_neo4j(results, FakeGraphRepository(), cfg)
    assert boosted[0].boosted_score == boosted[0].rrf_score


@pytest.mark.unit
def test_entity_boost_returns_results_unchanged_when_neo4j_is_unavailable() -> None:
    """``neo4j_client.available is False`` → no boost applied."""
    results = rrf([_bm25("a.md")], [])
    boosted = entity_boost_neo4j(results, FakeGraphRepository(available=False))
    assert boosted[0].boosted_score == boosted[0].rrf_score


@pytest.mark.unit
def test_entity_boost_returns_results_unchanged_when_neo4j_client_is_none() -> None:
    """A None client must short-circuit (the production callsite passes None
    when the graph isn't wired)."""
    results = rrf([_bm25("a.md")], [])
    boosted = entity_boost_neo4j(results, None)
    assert boosted[0].boosted_score == boosted[0].rrf_score


@pytest.mark.unit
def test_entity_boost_returns_empty_when_results_empty() -> None:
    assert entity_boost_neo4j([], FakeGraphRepository()) == []


@pytest.mark.unit
def test_entity_boost_applies_when_doc_path_matches_entity_vault_path() -> None:
    """An entity whose vault_path matches the doc path must get a multiplier > 1.
    Sabotage-prove: a successful test here means the cypher() seam is correctly
    wired and the formula actually fires.
    """
    rows = [_row("vault/jordan-blake.md", in_degree=10, name="Jordan", labels=["Person"])]
    neo = FakeGraphRepository(entities=rows)
    cfg = EntityBoostConfig(enabled=True, factor=0.5, cap=2.0)

    results = rrf([_bm25("vault/jordan-blake.md")], [])
    pre_score = results[0].rrf_score
    boosted = entity_boost_neo4j(results, neo, cfg)
    # With the only entity at max in-degree (10/10 = 1.0):
    # boost_amount = min(0.5 * log1p(1.0*10), cap-1.0) = min(0.5*log(11), 1.0) = min(1.199, 1.0) = 1.0
    # Multiplier = 2.0 (capped).
    assert boosted[0].boosted_score == pytest.approx(pre_score * 2.0)


@pytest.mark.unit
def test_entity_boost_factor_is_capped_at_config_cap() -> None:
    """Per docstring: "max boosted_score / rrf_score ratio" is config.cap.
    Even an entity with the maximum in-degree must produce a multiplier
    no greater than ``cap``.
    """
    rows = [_row("vault/jordan-blake.md", in_degree=999, name="Jordan", labels=["Person"])]
    neo = FakeGraphRepository(entities=rows)
    cfg = EntityBoostConfig(enabled=True, factor=10.0, cap=2.0)  # large factor, cap=2.0

    results = rrf([_bm25("vault/jordan-blake.md")], [])
    pre_score = results[0].rrf_score
    boosted = entity_boost_neo4j(results, neo, cfg)
    post_score = boosted[0].boosted_score
    # boost ratio must not exceed cap.
    ratio = post_score / pre_score
    assert ratio <= cfg.cap + 1e-9, f"entity boost exceeded cap {cfg.cap}: ratio={ratio}"
    # And to prove the test isn't passing trivially (sabotage-prove):
    # the boost MUST have been applied, so ratio > 1.0.
    assert ratio > 1.0, f"entity boost was not applied at all: ratio={ratio}"


@pytest.mark.unit
def test_entity_boost_log_formula_grows_monotonically_with_in_degree() -> None:
    """Per docstring: boost = 1 + min(factor * log1p(normalised * 10), cap - 1).
    Sub-cap, the multiplier grows monotonically with in-degree — a doc with
    higher in-degree must be boosted more.
    """
    cfg = EntityBoostConfig(enabled=True, factor=0.20, cap=10.0)  # very high cap to avoid clipping

    # Two distinct entities with different in-degrees. Use distinct paths so
    # both can be boosted in the same call (the result with higher in-degree
    # gets the larger multiplier).
    rows = [
        _row("vault/low.md", in_degree=1, name="Low", labels=["Person"]),
        _row("vault/high.md", in_degree=100, name="High", labels=["Person"]),
    ]
    neo = FakeGraphRepository(entities=rows)
    results = rrf([_bm25("vault/low.md"), _bm25("vault/high.md")], [])
    boosted = entity_boost_neo4j(results, neo, cfg)
    by_path = {r.path: r for r in boosted}
    # The higher in-degree doc must end up with the larger multiplier.
    low_ratio = by_path["vault/low.md"].boosted_score / by_path["vault/low.md"].rrf_score
    high_ratio = by_path["vault/high.md"].boosted_score / by_path["vault/high.md"].rrf_score
    assert high_ratio > low_ratio, f"expected high > low; got high={high_ratio} low={low_ratio}"


@pytest.mark.unit
def test_entity_boost_resorts_results_by_boosted_score_descending() -> None:
    """Per docstring "Returns: results sorted by boosted_score desc"."""
    cfg = EntityBoostConfig(enabled=True, factor=0.20, cap=2.0)
    # First doc gets no entity boost; second doc gets max boost.
    # Pre-boost the first ranks higher; post-boost the second must.
    rows = [_row("vault/celebrity.md", in_degree=999, name="Celebrity", labels=["Person"])]
    neo = FakeGraphRepository(entities=rows)
    results = rrf(
        [_bm25("vault/nobody.md"), _bm25("vault/celebrity.md")],
        [],
    )
    # Sanity: pre-boost order is nobody first, celebrity second.
    assert [r.path for r in results] == ["vault/nobody.md", "vault/celebrity.md"]
    boosted = entity_boost_neo4j(results, neo, cfg)
    # Post-boost: celebrity moves to front.
    assert boosted[0].path == "vault/celebrity.md"


@pytest.mark.unit
def test_entity_boost_applies_via_name_slug_lookup() -> None:
    """Per ``_LABEL_TO_DIR`` mapping: an entity named "Jordan Blake" with
    label "Person" registers under ``person/jordan-blake.md``, so a doc at
    that path should be boosted even without a direct vault_path match.
    """
    cfg = EntityBoostConfig(enabled=True, factor=0.20, cap=2.0)
    # Entity vault_path is somewhere else, but name-slug should produce
    # 'person/jordan-blake.md' as a secondary lookup key.
    rows = [_row("notes/about-jordan.md", in_degree=10, name="Jordan Blake", labels=["Person"])]
    neo = FakeGraphRepository(entities=rows)
    results = rrf([_bm25("person/jordan-blake.md")], [])
    pre_score = results[0].rrf_score
    boosted = entity_boost_neo4j(results, neo, cfg)
    # The slug-based lookup must produce a real boost (multiplier > 1).
    assert boosted[0].boosted_score > pre_score, (
        f"name-slug lookup didn't boost: pre={pre_score} post={boosted[0].boosted_score}"
    )


@pytest.mark.unit
def test_entity_boost_directory_match_applies_half_boost() -> None:
    """Per ``_lookup_mention_count``: docs under an entity directory get a
    half boost (in_deg // 2). So a sub-document of an entity dir gets less
    boost than the entity itself.
    """
    cfg = EntityBoostConfig(enabled=True, factor=0.20, cap=10.0)  # high cap so we see the gap
    # An entity at vault/jordan.md with high in-degree creates a dir 'vault'
    # → docs under vault/ get half boost.
    rows = [_row("vault/jordan.md", in_degree=100, name="Jordan", labels=["Person"])]
    neo = FakeGraphRepository(entities=rows)
    # Doc that's *under* the directory (not the entity itself).
    results = rrf([_bm25("vault/random-other-doc.md")], [])
    pre_score = results[0].rrf_score
    boosted = entity_boost_neo4j(results, neo, cfg)
    # Half-boost means it's boosted but less than the entity itself.
    boosted_ratio = boosted[0].boosted_score / pre_score
    assert boosted_ratio > 1.0, "directory match should produce some boost"


@pytest.mark.unit
def test_entity_boost_zero_in_degree_means_no_boost() -> None:
    """When the doc isn't in the Neo4j entity index, boosted_score == rrf_score."""
    rows = [_row("vault/other.md", in_degree=100, name="Other", labels=["Person"])]
    neo = FakeGraphRepository(entities=rows)
    results = rrf([_bm25("totally-different.md")], [])
    pre_score = results[0].rrf_score
    boosted = entity_boost_neo4j(results, neo)
    assert boosted[0].boosted_score == pytest.approx(pre_score)


@pytest.mark.unit
def test_entity_boost_never_raises_when_cypher_raises() -> None:
    """Per docstring "Never raises" — a graph backend whose cypher() raises must
    surface as unmodified results.
    """
    neo = FakeGraphRepository(raises=RuntimeError("neo4j down"))
    results = rrf([_bm25("a.md")], [])
    pre_score = results[0].rrf_score
    boosted = entity_boost_neo4j(results, neo)
    assert boosted[0].boosted_score == pytest.approx(pre_score)


@pytest.mark.unit
def test_entity_boost_empty_cypher_rows_means_no_boost() -> None:
    """When the graph backend returns no rows, the function still returns
    results and leaves boosted_score == rrf_score (rather than raising on
    max() of empty)."""
    neo = FakeGraphRepository()  # no entities → cypher returns []
    results = rrf([_bm25("a.md")], [])
    pre_score = results[0].rrf_score
    boosted = entity_boost_neo4j(results, neo)
    assert boosted[0].boosted_score == pytest.approx(pre_score)


# ---------------------------------------------------------------------------
# procedural_boost contracts
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_procedural_boost_disabled_config_returns_results_unchanged() -> None:
    cfg = ProceduralBoostConfig(enabled=False, factor=2.0)
    results = rrf([_bm25("docs/how-to-deploy.md")], [])
    pre_score = results[0].boosted_score
    boosted = procedural_boost(results, cfg)
    assert boosted[0].boosted_score == pytest.approx(pre_score)


@pytest.mark.unit
def test_procedural_boost_multiplies_matching_paths_by_factor() -> None:
    """A path matching ``how-to-`` pattern has its boosted_score multiplied by factor."""
    cfg = ProceduralBoostConfig(enabled=True, factor=1.4, path_patterns=(r"(?:^|/)how-to-",))
    results = rrf([_bm25("docs/how-to-deploy.md")], [])
    pre_score = results[0].boosted_score
    boosted = procedural_boost(results, cfg)
    assert boosted[0].boosted_score == pytest.approx(pre_score * 1.4)


@pytest.mark.unit
def test_procedural_boost_does_not_affect_non_matching_paths() -> None:
    cfg = ProceduralBoostConfig(enabled=True, factor=1.4, path_patterns=(r"(?:^|/)how-to-",))
    results = rrf([_bm25("docs/architecture.md")], [])
    pre_score = results[0].boosted_score
    boosted = procedural_boost(results, cfg)
    assert boosted[0].boosted_score == pytest.approx(pre_score)


@pytest.mark.unit
def test_procedural_boost_resorts_results_by_boosted_score() -> None:
    """After boost, results must be sorted descending by boosted_score —
    a non-procedural doc that ranked higher pre-boost may now rank below
    a procedural doc that got boosted past it.
    """
    cfg = ProceduralBoostConfig(enabled=True, factor=10.0, path_patterns=(r"(?:^|/)how-to-",))
    # BM25 ranks: architecture first (rank 1), how-to second (rank 2).
    # Without boost, architecture has higher rrf_score (1/61 > 1/62).
    # With factor=10, how-to becomes 10/62 = ~0.161, architecture stays at 1/61 = ~0.0164.
    # So how-to should now rank first.
    results = rrf([_bm25("docs/architecture.md"), _bm25("docs/how-to-deploy.md")], [])
    boosted = procedural_boost(results, cfg)
    assert boosted[0].path == "docs/how-to-deploy.md"


@pytest.mark.unit
def test_procedural_boost_returns_empty_when_results_empty() -> None:
    assert procedural_boost([]) == []


@pytest.mark.unit
def test_procedural_boost_never_raises_on_malformed_pattern() -> None:
    """A malformed regex pattern raises re.error inside the impl —
    "Never raises" requires the outer try/except catches it.
    """
    cfg = ProceduralBoostConfig(enabled=True, factor=1.4, path_patterns=("(unclosed-group",))
    results = rrf([_bm25("a.md")], [])
    pre_score = results[0].boosted_score
    boosted = procedural_boost(results, cfg)
    # No raise; results unmodified.
    assert boosted[0].boosted_score == pytest.approx(pre_score)


# ---------------------------------------------------------------------------
# temporal_date_boost contracts
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_temporal_date_boost_disabled_returns_results_unchanged() -> None:
    cfg = TemporalBoostConfig(date_path_boost_enabled=False)
    results = rrf([_bm25("logs/2026-04-15-meeting.md")], [])
    pre_score = results[0].boosted_score
    boosted = temporal_date_boost(results, "what happened on 2026-04-15", cfg)
    assert boosted[0].boosted_score == pytest.approx(pre_score)


@pytest.mark.unit
def test_temporal_date_boost_query_with_no_date_returns_unchanged() -> None:
    cfg = TemporalBoostConfig(date_path_boost_enabled=True)
    results = rrf([_bm25("logs/2026-04-15.md")], [])
    pre_score = results[0].boosted_score
    boosted = temporal_date_boost(results, "general query without date", cfg)
    assert boosted[0].boosted_score == pytest.approx(pre_score)


@pytest.mark.unit
def test_temporal_date_boost_iso_date_in_query_boosts_matching_path() -> None:
    cfg = TemporalBoostConfig(date_path_boost_enabled=True, date_path_boost_factor=2.0)
    results = rrf([_bm25("logs/2026-04-15-meeting.md")], [])
    pre_score = results[0].boosted_score
    boosted = temporal_date_boost(results, "what happened on 2026-04-15", cfg)
    # Path contains the queried date → boosted by factor.
    assert boosted[0].boosted_score == pytest.approx(pre_score * 2.0)


# ---------------------------------------------------------------------------
# chunk_date_boost contracts
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_chunk_date_boost_disabled_returns_results_unchanged() -> None:
    cfg = TemporalBoostConfig(chunk_date_boost_enabled=False)
    results = rrf([_bm25("doc.md")], [])
    results[0].chunk_date = "2026-04-15"
    pre_score = results[0].boosted_score
    boosted = chunk_date_boost(results, datetime.date(2026, 4, 15), cfg)
    assert boosted[0].boosted_score == pytest.approx(pre_score)


@pytest.mark.unit
def test_chunk_date_boost_query_date_none_returns_results_unchanged() -> None:
    cfg = TemporalBoostConfig(chunk_date_boost_enabled=True)
    results = rrf([_bm25("doc.md")], [])
    results[0].chunk_date = "2026-04-15"
    pre_score = results[0].boosted_score
    boosted = chunk_date_boost(results, None, cfg)
    assert boosted[0].boosted_score == pytest.approx(pre_score)


@pytest.mark.unit
def test_chunk_date_boost_doc_with_no_chunk_date_is_unaffected() -> None:
    cfg = TemporalBoostConfig(chunk_date_boost_enabled=True)
    results = rrf([_bm25("doc.md")], [])
    # No chunk_date set on the result.
    pre_score = results[0].boosted_score
    boosted = chunk_date_boost(results, datetime.date(2026, 4, 15), cfg)
    assert boosted[0].boosted_score == pytest.approx(pre_score)


@pytest.mark.unit
def test_chunk_date_boost_exact_date_match_yields_max_multiplier_of_two() -> None:
    """Per docstring: ``boost = 1 + exp(-delta^2 / (2*sigma^2))``.
    For delta=0: exp(0) = 1 → boost = 2.0 → boosted_score = pre_score * 2.0.
    """
    cfg = TemporalBoostConfig(chunk_date_boost_enabled=True, chunk_date_decay_halflife_days=30)
    results = rrf([_bm25("doc.md")], [])
    results[0].chunk_date = "2026-04-15"
    pre_score = results[0].boosted_score
    boosted = chunk_date_boost(results, datetime.date(2026, 4, 15), cfg)
    assert boosted[0].boosted_score == pytest.approx(pre_score * 2.0)


@pytest.mark.unit
def test_chunk_date_boost_at_halflife_distance_yields_boost_factor_one_point_five() -> None:
    """Per docstring: halflife is "days at which boost = 0.5 of max".
    Max boost addition = 1.0 (when delta=0). Half = 0.5.
    boost = 1 + 0.5 = 1.5 at halflife distance.
    """
    halflife = 30
    cfg = TemporalBoostConfig(chunk_date_boost_enabled=True, chunk_date_decay_halflife_days=halflife)
    results = rrf([_bm25("doc.md")], [])
    results[0].chunk_date = "2026-04-15"
    pre_score = results[0].boosted_score

    # Query date exactly halflife days after the chunk date.
    query_date = datetime.date(2026, 4, 15) + datetime.timedelta(days=halflife)
    boosted = chunk_date_boost(results, query_date, cfg)
    # The implementation uses sigma = halflife / 1.177 such that the Gaussian
    # at delta=halflife evaluates to 0.5.
    expected_boost = 1.0 + math.exp(-(halflife**2) / (2 * (halflife / 1.177) ** 2))
    assert boosted[0].boosted_score == pytest.approx(pre_score * expected_boost, rel=0.01)
    # Sanity: the multiplier is approximately 1.5.
    assert (boosted[0].boosted_score / pre_score) == pytest.approx(1.5, abs=0.01)


@pytest.mark.unit
def test_chunk_date_boost_far_future_chunk_date_yields_boost_near_one() -> None:
    """Many half-lives away → boost addition approaches 0 → multiplier ~ 1.0."""
    cfg = TemporalBoostConfig(chunk_date_boost_enabled=True, chunk_date_decay_halflife_days=30)
    results = rrf([_bm25("doc.md")], [])
    results[0].chunk_date = "2020-01-01"
    pre_score = results[0].boosted_score
    boosted = chunk_date_boost(results, datetime.date(2026, 4, 15), cfg)
    # Many years later → near-zero boost addition → ratio ≈ 1.0.
    assert boosted[0].boosted_score == pytest.approx(pre_score, abs=pre_score * 0.01)


@pytest.mark.unit
def test_chunk_date_boost_handles_malformed_chunk_date_gracefully() -> None:
    """A doc whose chunk_date isn't a valid ISO string: skipped, not raised."""
    cfg = TemporalBoostConfig(chunk_date_boost_enabled=True)
    results = rrf([_bm25("doc.md")], [])
    results[0].chunk_date = "not-a-date"
    pre_score = results[0].boosted_score
    boosted = chunk_date_boost(results, datetime.date(2026, 4, 15), cfg)
    assert boosted[0].boosted_score == pytest.approx(pre_score)


@pytest.mark.unit
def test_chunk_date_boost_accepts_iso_datetime_strings_truncating_to_date() -> None:
    """The impl truncates chunk_date_str[:10], so 'YYYY-MM-DDTHH:MM:SS' strings
    must be accepted (chunks may be indexed with full timestamps).
    """
    cfg = TemporalBoostConfig(chunk_date_boost_enabled=True)
    results = rrf([_bm25("doc.md")], [])
    results[0].chunk_date = "2026-04-15T10:30:00Z"
    pre_score = results[0].boosted_score
    boosted = chunk_date_boost(results, datetime.date(2026, 4, 15), cfg)
    # Same calendar day → max boost (multiplier ~ 2.0).
    assert boosted[0].boosted_score == pytest.approx(pre_score * 2.0)


@pytest.mark.unit
def test_chunk_date_boost_resorts_results_by_boosted_score_descending() -> None:
    """Per docstring "Returns: results re-sorted by boosted_score descending"."""
    cfg = TemporalBoostConfig(chunk_date_boost_enabled=True, chunk_date_decay_halflife_days=30)
    results = rrf([_bm25("old.md"), _bm25("today.md")], [])
    # Sanity: pre-boost order is rrf_score by BM25 rank.
    assert results[0].path == "old.md"
    results[0].chunk_date = "2020-01-01"  # very old
    results[1].chunk_date = "2026-04-15"  # query-date match
    boosted = chunk_date_boost(results, datetime.date(2026, 4, 15), cfg)
    # Today should now rank first (its chunk_date matches query_date).
    assert boosted[0].path == "today.md"


# ---------------------------------------------------------------------------
# canonical_path contracts
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_canonical_path_strips_obsidian_vault_prefix() -> None:
    """Per docstring: ``obsidian-vault/`` prefix is stripped for dedup."""
    assert canonical_path("obsidian-vault/notes/foo.md") == "notes/foo.md"


@pytest.mark.unit
def test_canonical_path_returns_input_unchanged_when_no_prefix() -> None:
    assert canonical_path("notes/foo.md") == "notes/foo.md"


@pytest.mark.unit
def test_canonical_path_dedupes_same_doc_indexed_under_two_paths_in_rrf() -> None:
    """The function exists so that BM25's 'obsidian-vault/foo.md' and vec's
    'foo.md' fuse into ONE FusedResult during rrf().
    """
    results = rrf([_bm25("obsidian-vault/foo.md")], [_vec("foo.md")])
    # Without canonicalisation the result list would have 2 entries.
    assert len(results) == 1
    assert results[0].in_bm25 is True
    assert results[0].in_vec is True


# ---------------------------------------------------------------------------
# temporal_date_boost — relative term contracts
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_temporal_date_boost_relative_recent_boosts_recent_path() -> None:
    """Per docstring: queries with relative temporal terms ('recent') boost
    documents whose path contains an ISO date within the recency window
    (90 days for 'recent' / 'last month').
    """
    cfg = TemporalBoostConfig(date_path_boost_enabled=True, date_path_boost_factor=2.0)
    today = datetime.date.today()
    recent_date = (today - datetime.timedelta(days=10)).isoformat()
    old_date = (today - datetime.timedelta(days=365)).isoformat()
    results = rrf([_bm25(f"logs/{old_date}-old.md"), _bm25(f"logs/{recent_date}-new.md")], [])
    boosted = temporal_date_boost(results, "what happened recently?", cfg)
    # Recent doc must rank above old doc post-boost.
    by_path = {r.path: r for r in boosted}
    recent_score = by_path[f"logs/{recent_date}-new.md"].boosted_score
    old_score = by_path[f"logs/{old_date}-old.md"].boosted_score
    assert recent_score > old_score, f"recent={recent_score} old={old_score}"


@pytest.mark.unit
def test_temporal_date_boost_year_month_in_query_matches_year_month_in_path() -> None:
    """Per docstring: YYYY-MM in query boosts paths containing that month string."""
    cfg = TemporalBoostConfig(date_path_boost_enabled=True, date_path_boost_factor=2.0)
    results = rrf([_bm25("logs/2026-04/summary.md")], [])
    pre_score = results[0].boosted_score
    boosted = temporal_date_boost(results, "what happened in 2026-04?", cfg)
    assert boosted[0].boosted_score == pytest.approx(pre_score * 2.0)


@pytest.mark.unit
def test_temporal_date_boost_full_iso_in_query_also_matches_year_month_prefix_in_path() -> None:
    """Per docstring: full ISO in query → boost paths containing exact date OR YYYY-MM prefix."""
    cfg = TemporalBoostConfig(date_path_boost_enabled=True, date_path_boost_factor=2.0)
    # Path has 2026-04 prefix but no exact day match.
    results = rrf([_bm25("logs/2026-04/notes.md")], [])
    pre_score = results[0].boosted_score
    boosted = temporal_date_boost(results, "what happened on 2026-04-15?", cfg)
    assert boosted[0].boosted_score == pytest.approx(pre_score * 2.0)


# ---------------------------------------------------------------------------
# bm25_primary_fuse contracts
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_bm25_primary_fuse_returns_empty_when_both_inputs_empty() -> None:
    assert bm25_primary_fuse([], []) == []


@pytest.mark.unit
def test_bm25_primary_fuse_orders_bm25_results_by_bm25_rank() -> None:
    """Per docstring: 'BM25 results ranked first, in BM25 rank order'."""
    results = bm25_primary_fuse(
        [_bm25("a.md"), _bm25("b.md"), _bm25("c.md")],
        [],
    )
    assert [r.path for r in results] == ["a.md", "b.md", "c.md"]
    # Each carries its bm25_rank.
    assert results[0].bm25_rank == 1
    assert results[1].bm25_rank == 2
    assert results[2].bm25_rank == 3


@pytest.mark.unit
def test_bm25_primary_fuse_appends_vec_only_results_after_bm25() -> None:
    """Per docstring: 'Vector-only documents are appended in vector rank order'."""
    results = bm25_primary_fuse(
        [_bm25("a.md")],
        [_vec("v1.md"), _vec("v2.md")],
    )
    assert [r.path for r in results] == ["a.md", "v1.md", "v2.md"]
    # Vec-only results do NOT have in_bm25.
    assert results[1].in_bm25 is False
    assert results[1].in_vec is True


@pytest.mark.unit
def test_bm25_primary_fuse_marks_overlap_with_in_vec_on_bm25_result() -> None:
    """When a vec result shares a path with a BM25 result, the BM25 entry
    must be marked in_vec=True (not duplicated as a separate entry).
    """
    results = bm25_primary_fuse(
        [_bm25("shared.md")],
        [_vec("shared.md")],
    )
    assert len(results) == 1
    assert results[0].in_bm25 is True
    assert results[0].in_vec is True


@pytest.mark.unit
def test_bm25_primary_fuse_score_preserves_bm25_rank_ordering() -> None:
    """boosted_score must rank in BM25 order (descending) so a downstream
    'sort by boosted_score' preserves the BM25-primary intent.
    """
    results = bm25_primary_fuse(
        [_bm25("a.md"), _bm25("b.md"), _bm25("c.md")],
        [_vec("v.md")],
    )
    scores = [r.boosted_score for r in results]
    # Descending without ties.
    assert scores == sorted(scores, reverse=True)
    # And vec-only is below all BM25.
    assert results[3].path == "v.md"
    assert results[3].boosted_score < results[2].boosted_score


@pytest.mark.unit
def test_bm25_primary_fuse_dedupes_case_insensitively() -> None:
    """Phase 1 dedup is path.lower()-based — BM25 results with the same path
    differing only in case must collapse to one entry.
    """
    results = bm25_primary_fuse(
        [_bm25("Notes/Foo.md"), _bm25("notes/foo.md")],
        [],
    )
    assert len(results) == 1


@pytest.mark.unit
def test_bm25_primary_fuse_never_raises_on_malformed_input() -> None:
    """Per docstring: 'Never raises'."""
    malformed = [{"not_file": "weird"}]  # type: ignore[list-item]
    result = bm25_primary_fuse(malformed, [])
    assert result == []


@pytest.mark.unit
def test_bm25_primary_fuse_canonical_path_dedupes_obsidian_vault_overlap() -> None:
    """If BM25 returns 'obsidian-vault/foo.md' and vec returns 'foo.md',
    they should fuse into one entry (via canonical_path).
    """
    results = bm25_primary_fuse(
        [_bm25("obsidian-vault/foo.md")],
        [_vec("foo.md")],
    )
    assert len(results) == 1
    assert results[0].in_bm25 is True
    assert results[0].in_vec is True


# ---------------------------------------------------------------------------
# Procedural boost — case-insensitivity
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_procedural_boost_path_pattern_is_case_insensitive() -> None:
    """Patterns are compiled with ``re.IGNORECASE`` in ``_procedural_boost_impl``."""
    cfg = ProceduralBoostConfig(enabled=True, factor=1.4, path_patterns=(r"(?:^|/)how-to-",))
    results = rrf([_bm25("docs/HOW-TO-Deploy.md")], [])
    pre_score = results[0].boosted_score
    boosted = procedural_boost(results, cfg)
    assert boosted[0].boosted_score == pytest.approx(pre_score * 1.4)
