"""End-to-end integration tests for the entity surface.

Wires the entity flow through multiple real kairix components — NER
(fake spaCy nlp), real filter chain, real ``suggest_entities`` /
``validate_entity`` / ``seed_graph``, and a writable ``FakeNeo4jClient``
at the system boundary.

What's covered here that unit + BDD don't catch:
  - The full suggest → validate → write-to-graph composition fires together
    (the unit tests stub each helper independently).
  - The graph write side (``seed_graph``) lands a per-entity ``upsert_node``
    call with the expected ``(label, id, props)`` triple.
  - Re-running ``suggest`` against the same input doesn't double-write —
    the validate/upsert path is idempotent on identity.
  - Wikidata validation projects matches into the structured envelope when
    the HTTP client is faked at the boundary (no real HTTP call).

Fakes:
  - ``_WritableFakeNeo4jClient`` — extends the canonical FakeNeo4jClient to
    record ``upsert_node`` calls and also satisfy ``cypher(MATCH (n {id})…)``
    for the validate-update path.
  - ``_fake_http_get`` — replaces ``requests.get`` (boundary HTTP) with a
    canned Wikidata-shaped response.
"""

from __future__ import annotations

from typing import Any

import pytest

from kairix.knowledge.entities.seed import EntityCandidate, seed_graph
from kairix.knowledge.entities.suggest import suggest_entities
from kairix.knowledge.entities.validate import validate_entity
from kairix.use_cases.entity import (
    EntitySuggestDeps,
    EntityValidateDeps,
    run_entity_suggest,
    run_entity_validate,
)
from tests.fixtures.neo4j_mock import FakeNeo4jClient

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Boundary fakes
# ---------------------------------------------------------------------------


class _WritableFakeNeo4jClient(FakeNeo4jClient):
    """FakeNeo4jClient that also records SET cypher calls so the
    validate-update path can be asserted end-to-end."""

    def __init__(self, entities: list[dict] | None = None) -> None:
        super().__init__(entities=entities)
        self.set_calls: list[tuple[str, dict[str, Any] | None]] = []

    def cypher(self, query: str, params: dict | None = None) -> list[dict]:
        # Intercept the validate-update write so the integration test can
        # confirm wikidata_qid actually lands. Reading queries delegate to
        # the parent's pattern-matched fixture.
        if "SET" in query and "wikidata_qid" in query:
            self.set_calls.append((query, params))
            return []
        return super().cypher(query, params)


class _FakeNerEntity:
    def __init__(self, text: str, label: str) -> None:
        self.text = text
        self.label_ = label


class _FakeNerSentence:
    def __init__(self, text: str, ents: list[_FakeNerEntity]) -> None:
        self.text = text
        self.ents = ents


class _FakeNerDoc:
    def __init__(self, sents: list[_FakeNerSentence]) -> None:
        self.sents = sents


class _FakeNlpPipeline:
    """Tiny spaCy-shaped pipeline: returns the configured doc on every call."""

    def __init__(self, sentences: list[tuple[str, list[tuple[str, str]]]]) -> None:
        self._doc = _FakeNerDoc(
            sents=[
                _FakeNerSentence(
                    text=sent_text,
                    ents=[_FakeNerEntity(t, lbl) for t, lbl in ents],
                )
                for sent_text, ents in sentences
            ]
        )

    def __call__(self, text: str) -> _FakeNerDoc:
        del text
        return self._doc


class _FakeHttpResponse:
    """``requests.Response``-shaped object for ``validate_entity``'s http_get seam."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        """Match the requests API surface — never raises in this fake."""

    def json(self) -> dict[str, Any]:
        return dict(self._payload)


def _fake_http_get_acme(*_args: Any, **_kwargs: Any) -> Any:
    """Return a Wikidata-shaped response carrying one ``Acme Corp`` match.

    Typed as ``Any`` so the call-site duck-types onto
    ``Callable[..., requests.Response]`` — the production code only ever
    invokes ``raise_for_status()`` and ``json()`` on the return value.
    """
    return _FakeHttpResponse(
        {
            "search": [
                {
                    "id": "Q12345",
                    "label": "Acme Corp",
                    "description": "Fictional supplier",
                }
            ]
        }
    )


# ---------------------------------------------------------------------------
# End-to-end suggest → validate → write
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_suggest_then_seed_lands_entity_in_graph_with_expected_shape() -> None:
    """Full happy path: NER finds a new entity, ``seed_graph`` upserts the
    card into Neo4j with ``(label, id, props)`` matching the suggestion.

    Sabotage: if ``seed_graph`` stopped passing ``c.name`` through to
    ``props``, the asserted ``"name": "Acme Corp"`` key would vanish.
    """
    neo4j = _WritableFakeNeo4jClient(entities=[])
    nlp = _FakeNlpPipeline([("Acme Corp launched a product.", [("Acme Corp", "ORG")])])

    def _real_suggest(text: str, client: Any) -> list[Any]:
        return suggest_entities(text, client, nlp=nlp)

    deps = EntitySuggestDeps(suggest_fn=_real_suggest, neo4j_client_fn=lambda: neo4j)
    out = run_entity_suggest("Acme Corp launched a product.", deps=deps)

    assert out.error == ""
    assert [s.text for s in out.suggestions] == ["Acme Corp"]
    assert out.suggestions[0].is_new is True

    # Translate the suggestion into a seed candidate and write it via the
    # production seed_graph path.
    candidate = EntityCandidate(
        name=out.suggestions[0].text,
        entity_type="Organisation",
        confidence=0.9,
        source_docs=["docs/launch.md"],
    )
    written = seed_graph(neo4j, [candidate])

    assert written == 1
    assert len(neo4j.upsert_node_calls) == 1
    call = neo4j.upsert_node_calls[0]
    # seed_graph calls client.upsert_node(entity_type, suggested_id, props)
    assert call["args"][0] == "Organisation"
    assert call["args"][1] == "acme-corp"
    props = call["args"][2]
    assert props["name"] == "Acme Corp"
    assert props["source_docs"] == ["docs/launch.md"]


@pytest.mark.integration
def test_repeat_suggest_is_idempotent_against_seeded_graph() -> None:
    """Once an entity is seeded, a second suggest cycle marks it
    ``is_new=False`` and a re-run of ``seed_graph`` only calls
    ``upsert_node`` once more (Neo4j MERGE-style idempotence, modelled
    via the fake recording one call per pass).

    Sabotage: if the suggest path stopped consulting ``find_by_name``
    after a seed, the second pass would re-report ``is_new=True``.
    """
    seeded = [
        {
            "id": "acme-corp",
            "name": "Acme Corp",
            "label": "Organisation",
            "vault_path": "entities/acme-corp.md",
            "summary": "Fictional supplier",
        }
    ]
    neo4j = _WritableFakeNeo4jClient(entities=seeded)
    nlp = _FakeNlpPipeline([("Acme Corp shipped v2.", [("Acme Corp", "ORG")])])

    def _real_suggest(text: str, client: Any) -> list[Any]:
        return suggest_entities(text, client, nlp=nlp)

    deps = EntitySuggestDeps(suggest_fn=_real_suggest, neo4j_client_fn=lambda: neo4j)

    # First pass — entity exists, suggester reports existing id.
    first = run_entity_suggest("Acme Corp shipped v2.", deps=deps)
    assert first.suggestions[0].is_new is False
    assert first.suggestions[0].existing_id == "acme-corp"

    # Second pass — same answer, same identity.
    second = run_entity_suggest("Acme Corp shipped v2.", deps=deps)
    assert second.suggestions[0].is_new is False
    assert second.suggestions[0].existing_id == "acme-corp"

    # Re-seeding the same candidate calls upsert_node again (idempotent on
    # identity — Neo4j MERGE upserts; the fake records the call but the row
    # doesn't duplicate).
    candidate = EntityCandidate(
        name="Acme Corp",
        entity_type="Organisation",
        confidence=0.9,
        source_docs=["docs/launch.md"],
    )
    seed_graph(neo4j, [candidate])
    seed_graph(neo4j, [candidate])

    # Two seed calls, two recorded upserts — but the ids match, so the
    # production MERGE behaviour collapses to one row server-side.
    assert len(neo4j.upsert_node_calls) == 2
    assert all(c["args"][1] == "acme-corp" for c in neo4j.upsert_node_calls)


@pytest.mark.integration
def test_validate_against_fake_wikidata_writes_qid_to_graph() -> None:
    """End-to-end validate: known entity → Wikidata search (fake HTTP) →
    high-confidence match → ``wikidata_qid`` written to Neo4j.

    Sabotage: if ``validate_entity`` stopped invoking ``cypher(... SET ...
    wikidata_qid ...)`` on a high-confidence match, ``set_calls`` would
    stay empty and the assertion would fail.
    """
    seeded = [
        {
            "id": "acme-corp",
            "name": "Acme Corp",
            "label": "Organisation",
            "vault_path": "entities/acme-corp.md",
            "summary": "Fictional supplier",
        }
    ]
    neo4j = _WritableFakeNeo4jClient(entities=seeded)

    def _real_validate(name: str, client: Any, update: bool) -> dict[str, Any]:
        return validate_entity(name, client, update=update, http_get=_fake_http_get_acme)

    deps = EntityValidateDeps(
        validate_fn=_real_validate,
        neo4j_client_fn=lambda: neo4j,
    )
    out = run_entity_validate("Acme Corp", update=True, deps=deps)

    assert out.error == ""
    assert out.neo4j_id == "acme-corp"
    assert len(out.matches) == 1
    assert out.matches[0].qid == "Q12345"
    assert out.matches[0].confidence == "high"
    assert out.updated is True
    # The validate path's write actually fired through to cypher.
    assert len(neo4j.set_calls) == 1
    _, params = neo4j.set_calls[0]
    assert params == {"id": "acme-corp", "qid": "Q12345"}


@pytest.mark.integration
def test_suggest_drops_role_phrase_through_default_chain() -> None:
    """The default filter chain wired into ``suggest_entities`` drops
    role phrases mistagged as ORG — the multi-component path
    (NER → ChainedSuggestionFilter → Neo4j lookup) cooperates.

    Sabotage: if the filter chain were bypassed, the role phrase would
    surface and ``out.suggestions`` would be non-empty.
    """
    neo4j = _WritableFakeNeo4jClient(entities=[])
    nlp = _FakeNlpPipeline(
        [
            (
                "the regional team is hiring engineers.",
                [("the regional team", "ORG")],
            )
        ]
    )

    def _real_suggest(text: str, client: Any) -> list[Any]:
        return suggest_entities(text, client, nlp=nlp)

    deps = EntitySuggestDeps(suggest_fn=_real_suggest, neo4j_client_fn=lambda: neo4j)
    out = run_entity_suggest("the regional team is hiring engineers.", deps=deps)

    assert out.error == ""
    assert out.suggestions == []
    assert out.new_count == 0
    assert out.existing_count == 0
