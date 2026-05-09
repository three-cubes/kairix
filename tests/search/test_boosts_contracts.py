"""
Contract probes for kairix.core.search.boosts.

Each test is a probe of one documented claim from boosts.py. Tests use the
canonical fakes from tests/fakes.py — no monkeypatching, no inline stubs,
no @patch.

Probes are organised by the strategy they cover:

  EntityBoost
    - Requires GraphRepository (Protocol).
    - Documents matching entity vault paths receive a log-scaled boost
      proportional to in-degree.
    - When graph is unavailable: results returned unmodified (boosted_score
      = rrf_score).
    - When config disables: results returned unmodified.

  ProceduralBoost
    - Documents matching procedural patterns get boosted_score *= factor.
    - Returns sorted by boosted_score descending after applying.
    - When config disables: results returned unmodified.

  TemporalDateBoost
    - Disabled by default (date_path_boost_enabled=False).
    - When enabled and query has explicit date: documents with date in path
      get boosted_score *= date_path_boost_factor.

  ChunkDateBoost
    - Reads context["query_date"]; None = no-op.
    - Uses Gaussian decay: closer chunk_date = larger boost.
    - When config disables (chunk_date_boost_enabled=False): no-op.
    - Disabled by default.

All tests run with @pytest.mark.contract.
"""

from __future__ import annotations

import datetime
import math

import pytest

from kairix.core.protocols import BoostStrategy, GraphRepository
from kairix.core.search.boosts import (
    ChunkDateBoost,
    EntityBoost,
    ProceduralBoost,
    TemporalDateBoost,
)
from kairix.core.search.config import (
    EntityBoostConfig,
    ProceduralBoostConfig,
    TemporalBoostConfig,
)
from kairix.core.search.rrf import FusedResult
from tests.fakes import FakeGraphRepository

pytestmark = pytest.mark.contract


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _result(
    path: str,
    score: float = 0.5,
    chunk_date: str = "",
) -> FusedResult:
    """Build a FusedResult whose rrf_score and boosted_score are equal.

    Mirrors the state after rrf() initialises both fields.
    """
    r = FusedResult(
        path=path,
        collection="c",
        title="T",
        snippet="s",
        rrf_score=score,
        boosted_score=score,
    )
    r.chunk_date = chunk_date
    return r


# ---------------------------------------------------------------------------
# EntityBoost — protocol + behaviour
# ---------------------------------------------------------------------------


class TestEntityBoostProtocolCompliance:
    """EntityBoost satisfies BoostStrategy via runtime_checkable Protocol."""

    def test_satisfies_boost_strategy_protocol(self) -> None:
        graph: GraphRepository = FakeGraphRepository(available=False)
        assert isinstance(EntityBoost(graph=graph), BoostStrategy)

    def test_accepts_optional_config(self) -> None:
        """The constructor accepts an EntityBoostConfig."""
        graph: GraphRepository = FakeGraphRepository(available=False)
        cfg = EntityBoostConfig(enabled=False)
        b = EntityBoost(graph=graph, config=cfg)
        # Sanity: invocation does not raise with the disabled config.
        assert b.boost([], "q", {}) == []


class TestEntityBoostDocumentedClaims:
    """One contract probe per claim from EntityBoost.__doc__ + boost docstring."""

    def test_empty_results_returns_empty(self) -> None:
        """Claim: 'All functions return [] on empty inputs. Never raise.'"""
        graph: GraphRepository = FakeGraphRepository(available=True)
        b = EntityBoost(graph=graph)
        assert b.boost([], "anything", {}) == []

    def test_graph_unavailable_returns_unmodified(self) -> None:
        """Claim (rrf.py): 'if Neo4j is unavailable the boost is skipped and
        results are returned unmodified'.

        The fake graph reports available=False, so each result keeps
        boosted_score == rrf_score.
        """
        graph: GraphRepository = FakeGraphRepository(available=False)
        b = EntityBoost(graph=graph)

        results = [
            _result("concept/openclaw.md", score=0.3),
            _result("notes/random.md", score=0.4),
        ]
        out = b.boost(results, "openclaw", {})

        for r in out:
            assert r.boosted_score == r.rrf_score, (
                f"unavailable graph must not modify boosted_score; got {r.boosted_score} vs {r.rrf_score} for {r.path}"
            )

    def test_config_disabled_returns_unmodified(self) -> None:
        """Claim: EntityBoostConfig.enabled = False short-circuits."""
        graph: GraphRepository = FakeGraphRepository(
            entities=[
                {
                    "name": "OpenClaw",
                    "vault_path": "concept/openclaw.md",
                    "labels": ["concept"],
                    "in_degree": 5,
                }
            ],
            available=True,
        )
        cfg = EntityBoostConfig(enabled=False)
        b = EntityBoost(graph=graph, config=cfg)

        results = [_result("concept/openclaw.md", score=0.5)]
        out = b.boost(results, "openclaw", {})

        assert out[0].boosted_score == pytest.approx(0.5)
        assert out[0].boosted_score == pytest.approx(out[0].rrf_score)

    def test_matching_entity_gets_boosted(self) -> None:
        """Claim: 'Documents matching entity vault paths receive a log-scaled
        boost proportional to in-degree.'

        Sabotage-prove: if entity-path lookup is broken, boosted_score will
        equal rrf_score and this assertion fails.
        """
        graph: GraphRepository = FakeGraphRepository(
            entities=[
                {
                    "name": "OpenClaw",
                    "vault_path": "concept/openclaw.md",
                    "labels": ["concept"],
                    "in_degree": 7,
                }
            ],
            available=True,
        )
        b = EntityBoost(graph=graph)

        results = [_result("concept/openclaw.md", score=0.5)]
        out = b.boost(results, "openclaw", {})

        # Direct sabotage-prove value: a strict >, not >=
        assert out[0].boosted_score > 0.5, (
            f"matching entity must be boosted strictly above rrf_score, got {out[0].boosted_score}"
        )
        assert out[0].entity_mention_count == 7

    def test_non_matching_entity_unchanged(self) -> None:
        """Claim: 'Documents matching an entity vault_path or living inside
        an entity directory receive a log-scaled boost ...'

        Documents that do NOT match get boosted_score == rrf_score.
        """
        graph: GraphRepository = FakeGraphRepository(
            entities=[
                {
                    "name": "OpenClaw",
                    "vault_path": "concept/openclaw.md",
                    "labels": ["concept"],
                    "in_degree": 7,
                }
            ],
            available=True,
        )
        b = EntityBoost(graph=graph)

        results = [_result("notes/something_else.md", score=0.5)]
        out = b.boost(results, "openclaw", {})

        assert out[0].boosted_score == pytest.approx(0.5)
        assert out[0].entity_mention_count == 0

    def test_returns_sorted_by_boosted_score_descending(self) -> None:
        """Implicit claim from rrf.entity_boost_neo4j docstring: results
        come back sorted by boosted_score descending.
        """
        graph: GraphRepository = FakeGraphRepository(
            entities=[
                {
                    "name": "OpenClaw",
                    "vault_path": "concept/openclaw.md",
                    "labels": ["concept"],
                    "in_degree": 50,
                }
            ],
            available=True,
        )
        b = EntityBoost(graph=graph)

        # Both start equal; entity match wins.
        results = [
            _result("notes/other.md", score=0.5),
            _result("concept/openclaw.md", score=0.5),
        ]
        out = b.boost(results, "openclaw", {})

        scores = [r.boosted_score for r in out]
        assert scores == sorted(scores, reverse=True), f"output not sorted desc: {scores}"
        assert out[0].path == "concept/openclaw.md"

    def test_boost_capped_at_config_cap(self) -> None:
        """Claim from rrf.py module docstring:
            boost(d) = 1 + min(factor * log(1 + mention_count), cap - 1)

        With a tiny cap (1.5), boost ratio cannot exceed 1.5 even for huge
        in-degree.
        """
        graph: GraphRepository = FakeGraphRepository(
            entities=[
                {
                    "name": "Mega",
                    "vault_path": "concept/mega.md",
                    "labels": ["concept"],
                    "in_degree": 10_000,
                }
            ],
            available=True,
        )
        cfg = EntityBoostConfig(enabled=True, factor=10.0, cap=1.5)
        b = EntityBoost(graph=graph, config=cfg)

        results = [_result("concept/mega.md", score=1.0)]
        out = b.boost(results, "mega", {})

        # boosted_score / rrf_score must be <= cap
        assert out[0].boosted_score / out[0].rrf_score <= 1.5 + 1e-9


# ---------------------------------------------------------------------------
# ProceduralBoost — protocol + behaviour
# ---------------------------------------------------------------------------


class TestProceduralBoostProtocolCompliance:
    def test_satisfies_boost_strategy_protocol(self) -> None:
        assert isinstance(ProceduralBoost(), BoostStrategy)

    def test_default_config_constructs(self) -> None:
        """Default constructor uses ProceduralBoostConfig() defaults."""
        b = ProceduralBoost()
        # Sanity: empty input returns empty.
        assert b.boost([], "q", {}) == []


class TestProceduralBoostDocumentedClaims:
    def test_empty_results_returns_empty(self) -> None:
        """Empty input: returns []. Never raises."""
        assert ProceduralBoost().boost([], "q", {}) == []

    def test_config_disabled_returns_unmodified(self) -> None:
        """Claim: ProceduralBoostConfig.enabled=False is a hard-off."""
        b = ProceduralBoost(config=ProceduralBoostConfig(enabled=False))
        results = [_result("how-to-deploy.md", score=0.5)]
        out = b.boost(results, "deploy", {})

        # Hard-off: boosted_score is whatever the input was — not multiplied.
        assert out[0].boosted_score == pytest.approx(0.5)

    def test_procedural_path_pattern_match_boosts(self) -> None:
        """Claim: 'Multiplies boosted_score by config.factor for documents
        whose path matches procedural patterns.'

        Sabotage-prove: if the path regex never matches, boosted_score won't
        change and this strict-> fails.
        """
        b = ProceduralBoost()
        results = [_result("guides/how-to-deploy.md", score=0.5)]
        out = b.boost(results, "how to deploy", {})

        cfg = ProceduralBoostConfig()
        assert out[0].boosted_score == pytest.approx(0.5 * cfg.factor)

    def test_non_procedural_path_not_boosted(self) -> None:
        """Documents not matching procedural patterns are unaffected."""
        b = ProceduralBoost()
        results = [_result("notes/general-musings.md", score=0.5)]
        out = b.boost(results, "how to deploy", {})

        assert out[0].boosted_score == pytest.approx(0.5)

    def test_returns_sorted_by_boosted_score_descending(self) -> None:
        """Output is sorted by boosted_score descending after multiplication."""
        b = ProceduralBoost()
        results = [
            _result("notes/highscore_but_not_proc.md", score=0.6),
            _result("guides/how-to-x.md", score=0.5),
        ]
        out = b.boost(results, "how to x", {})

        scores = [r.boosted_score for r in out]
        assert scores == sorted(scores, reverse=True), f"output not sorted desc: {scores}"

    def test_custom_factor_applied(self) -> None:
        """Custom config.factor is honoured (sabotage: hardcoding 1.4 would fail)."""
        cfg = ProceduralBoostConfig(factor=2.5)
        b = ProceduralBoost(config=cfg)
        results = [_result("how-to-x.md", score=0.4)]
        out = b.boost(results, "how to x", {})

        assert out[0].boosted_score == pytest.approx(0.4 * 2.5)


# ---------------------------------------------------------------------------
# TemporalDateBoost — protocol + behaviour
# ---------------------------------------------------------------------------


class TestTemporalDateBoostProtocolCompliance:
    def test_satisfies_boost_strategy_protocol(self) -> None:
        assert isinstance(TemporalDateBoost(), BoostStrategy)


class TestTemporalDateBoostDocumentedClaims:
    def test_empty_results_returns_empty(self) -> None:
        assert TemporalDateBoost().boost([], "yesterday", {}) == []

    def test_disabled_by_default_no_op(self) -> None:
        """Claim from TemporalBoostConfig: 'date_path_boost_enabled: bool = False'.

        Default-constructed TemporalDateBoost does not modify scores even
        when the query contains a matching date.
        """
        b = TemporalDateBoost()
        results = [_result("daily/2026-04-15.md", score=0.5)]
        out = b.boost(results, "2026-04-15 standup", {})

        assert out[0].boosted_score == pytest.approx(0.5), "disabled-by-default boost must NOT modify scores"

    def test_enabled_with_iso_date_in_query_boosts_matching_path(self) -> None:
        """Claim from boosts.py: 'Boosts documents with explicit date strings
        ... matching the query.'

        Sabotage-prove: if the date isn't extracted from the query, no boost
        is applied and the strict-> fails.
        """
        cfg = TemporalBoostConfig(date_path_boost_enabled=True, date_path_boost_factor=1.5)
        b = TemporalDateBoost(config=cfg)
        # Use a non-matching path in a different month so the YYYY-MM
        # prefix-fallback inside _extract_query_date_strings doesn't double-match.
        results = [
            _result("daily/2026-04-15.md", score=0.5),
            _result("daily/2025-09-01.md", score=0.5),
        ]
        out = b.boost(results, "what happened on 2026-04-15", {})

        match = next(r for r in out if "2026-04-15" in r.path)
        non_match = next(r for r in out if "2025-09-01" in r.path)
        assert match.boosted_score > non_match.boosted_score
        assert match.boosted_score == pytest.approx(0.5 * 1.5)
        assert non_match.boosted_score == pytest.approx(0.5)

    def test_enabled_no_temporal_terms_no_op(self) -> None:
        """When the query has neither an ISO date nor a relative term, no
        boost is applied even when the boost is enabled.
        """
        cfg = TemporalBoostConfig(date_path_boost_enabled=True)
        b = TemporalDateBoost(config=cfg)
        results = [_result("daily/2026-04-15.md", score=0.5)]
        out = b.boost(results, "architecture decision", {})

        assert out[0].boosted_score == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# ChunkDateBoost — protocol + behaviour
# ---------------------------------------------------------------------------


class TestChunkDateBoostProtocolCompliance:
    def test_satisfies_boost_strategy_protocol(self) -> None:
        assert isinstance(ChunkDateBoost(), BoostStrategy)


class TestChunkDateBoostDocumentedClaims:
    def test_empty_results_returns_empty(self) -> None:
        assert ChunkDateBoost().boost([], "q", {}) == []

    def test_disabled_by_default_no_op(self) -> None:
        """Claim from TemporalBoostConfig: 'chunk_date_boost_enabled: bool = False'."""
        b = ChunkDateBoost()
        qdate = datetime.date(2026, 4, 17)
        results = [_result("notes/x.md", score=0.5, chunk_date="2026-04-15")]
        out = b.boost(results, "q", {"query_date": qdate})

        assert out[0].boosted_score == pytest.approx(0.5)

    def test_no_query_date_in_context_no_op(self) -> None:
        """Claim from boost docstring: 'query_date: Date extracted from the
        query (datetime.date). None = no-op.'

        ChunkDateBoost reads context['query_date']; when missing, it's None
        and the boost is a no-op.
        """
        cfg = TemporalBoostConfig(chunk_date_boost_enabled=True)
        b = ChunkDateBoost(config=cfg)
        results = [_result("notes/x.md", score=0.5, chunk_date="2026-04-15")]
        out = b.boost(results, "q", {})  # no query_date

        assert out[0].boosted_score == pytest.approx(0.5)

    def test_enabled_recent_chunk_date_boosted(self) -> None:
        """Claim from boost docstring: 'Uses Gaussian decay based on the
        distance between chunk_date and the query date.'

        Sabotage-prove: at delta=0 the boost is exactly 2.0 (1 + exp(0)).
        """
        cfg = TemporalBoostConfig(chunk_date_boost_enabled=True)
        b = ChunkDateBoost(config=cfg)
        qdate = datetime.date(2026, 4, 17)
        results = [_result("notes/today.md", score=0.5, chunk_date="2026-04-17")]
        out = b.boost(results, "q", {"query_date": qdate})

        # Boost at delta=0 = 1 + exp(0) = 2.0
        assert out[0].boosted_score == pytest.approx(0.5 * 2.0)

    def test_enabled_recent_beats_old(self) -> None:
        """Recent chunk_date documents beat older ones when boosted."""
        cfg = TemporalBoostConfig(chunk_date_boost_enabled=True)
        b = ChunkDateBoost(config=cfg)
        qdate = datetime.date(2026, 4, 17)
        results = [
            _result("notes/recent.md", score=0.5, chunk_date="2026-04-15"),
            _result("notes/old.md", score=0.5, chunk_date="2024-01-01"),
        ]
        out = b.boost(results, "q", {"query_date": qdate})

        recent = next(r for r in out if "recent" in r.path)
        old = next(r for r in out if "old" in r.path)
        assert recent.boosted_score > old.boosted_score

    def test_halflife_is_honoured(self) -> None:
        """Claim: 'sigma = halflife / 1.177 (halflife = days at which boost
        = 0.5 of max).'

        At delta == halflife days the multiplicative boost should be ~1.5
        (1 + 0.5).
        """
        cfg = TemporalBoostConfig(
            chunk_date_boost_enabled=True,
            chunk_date_decay_halflife_days=30,
        )
        b = ChunkDateBoost(config=cfg)
        qdate = datetime.date(2026, 4, 17)
        chunk = (qdate - datetime.timedelta(days=30)).isoformat()
        results = [_result("notes/m.md", score=1.0, chunk_date=chunk)]
        out = b.boost(results, "q", {"query_date": qdate})

        # Boost ~ 1.5 ± a little for floating point
        sigma = 30 / 1.177
        expected = 1.0 + math.exp(-(30**2) / (2 * sigma**2))
        assert out[0].boosted_score == pytest.approx(expected, rel=1e-6)


# ---------------------------------------------------------------------------
# Cross-cutting: never-raises contract
# ---------------------------------------------------------------------------


class TestBoostsNeverRaise:
    """The module-level docstring of rrf.py says 'All functions ... Never raise.'
    The boosts wrappers must preserve this contract.
    """

    def test_entity_boost_with_unavailable_graph_does_not_raise(self) -> None:
        graph: GraphRepository = FakeGraphRepository(available=False)
        b = EntityBoost(graph=graph)
        # Must not raise — must just return results unchanged.
        results = [_result("anywhere.md", score=0.5)]
        out = b.boost(results, "weird query !@#$", {})
        assert out[0].boosted_score == pytest.approx(0.5)

    def test_procedural_boost_garbage_query_does_not_raise(self) -> None:
        b = ProceduralBoost()
        out = b.boost([_result("how-to-x.md", score=0.5)], "!@#$%^&*()", {})
        assert len(out) == 1

    def test_temporal_date_boost_garbage_query_does_not_raise(self) -> None:
        cfg = TemporalBoostConfig(date_path_boost_enabled=True)
        b = TemporalDateBoost(config=cfg)
        out = b.boost([_result("daily/2026-01-01.md", score=0.5)], "", {})
        assert len(out) == 1

    def test_chunk_date_boost_with_bad_chunk_date_does_not_raise(self) -> None:
        cfg = TemporalBoostConfig(chunk_date_boost_enabled=True)
        b = ChunkDateBoost(config=cfg)
        results = [_result("notes/x.md", score=0.5, chunk_date="not-a-date")]
        out = b.boost(results, "q", {"query_date": datetime.date(2026, 4, 17)})
        # Bad chunk_date: skipped, score unchanged
        assert out[0].boosted_score == pytest.approx(0.5)
