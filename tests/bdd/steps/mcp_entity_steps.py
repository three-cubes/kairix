"""Step definitions for mcp_agent_entity.feature."""

from __future__ import annotations

from pytest_bdd import given, parsers, then, when

from tests.fixtures.neo4j_mock import FakeNeo4jClient

# Module-level state (simple, test-scoped)
_state: dict = {}


class _EntityAwareFakeNeo4j(FakeNeo4jClient):
    """FakeNeo4jClient subclass that handles the cypher query used by _fetch_entity_card.

    _fetch_entity_card queries by id (slug) or name and expects rows with
    keys: type, id, name, summary, vault_path.  The base FakeNeo4jClient.cypher
    returns raw entity dicts which use 'label' instead of 'type'.  This subclass
    intercepts the MATCH query and returns correctly-shaped rows.
    """

    def cypher(self, query: str, params: dict | None = None) -> list[dict]:
        if params and ("id" in params or "name" in params):
            target_id = (params.get("id") or "").lower()
            target_name = (params.get("name") or "").lower()
            for e in self._entities:
                aliases = [a.lower() for a in (e.get("aliases") or [])]
                if (
                    e.get("id", "").lower() == target_id
                    or e.get("name", "").lower() == target_name
                    or target_name in aliases
                ):
                    return [
                        {
                            "type": e.get("label", ""),
                            "id": e.get("id", ""),
                            "name": e.get("name", ""),
                            "vault_path": e.get("vault_path", ""),
                            "role": e.get("role", ""),
                            "org": e.get("org", ""),
                            "tier": e.get("tier", ""),
                            "engagement_status": e.get("engagement_status", ""),
                            "domain": e.get("domain", ""),
                            "industry": e.get("industry", ""),
                            "category": e.get("category", ""),
                        }
                    ]
            return []
        return super().cypher(query, params)


def _patch_neo4j(fake_client: FakeNeo4jClient) -> None:
    """Store the fake client and patcher so the @when step can activate it."""
    _state["fake_neo4j"] = fake_client


@given(parsers.parse('Neo4j has entity "{name}" of type "{etype}" with summary "{summary}"'))
def neo4j_has_entity(name, etype, summary):
    from kairix.utils import slugify

    entity = {
        "id": slugify(name),
        "name": name,
        "label": etype,
        "vault_path": f"entities/{slugify(name)}.md",
        "role": summary,
    }
    _patch_neo4j(_EntityAwareFakeNeo4j(entities=[entity]))


@given(parsers.parse('Neo4j has no entity named "{name}"'))
def neo4j_has_no_entity(name):
    _patch_neo4j(_EntityAwareFakeNeo4j(entities=[]))


@when(parsers.re(r'the agent calls tool_entity with name "(?P<name>[^"]*)"'))
def call_tool_entity(name):
    from kairix.agents.mcp.server import tool_entity

    _state["exception"] = None
    _state["result"] = None
    fake = _state.get("fake_neo4j", _EntityAwareFakeNeo4j(entities=[]))
    try:
        _state["result"] = tool_entity(name=name, neo4j_client=fake)
    except Exception as exc:
        _state["exception"] = exc


@then(parsers.parse('the entity response has name "{expected}"'))
def entity_has_name(expected):
    assert _state["exception"] is None, f"tool_entity raised: {_state['exception']}"
    assert _state["result"]["name"] == expected, f"Expected name {expected!r}, got {_state['result']['name']!r}"


@then(parsers.parse('the entity response has type "{expected}"'))
def entity_has_type(expected):
    assert _state["exception"] is None, f"tool_entity raised: {_state['exception']}"
    assert _state["result"]["type"] == expected, f"Expected type {expected!r}, got {_state['result']['type']!r}"


@then("the entity response has a non-empty summary")
def entity_has_nonempty_summary():
    assert _state["exception"] is None, f"tool_entity raised: {_state['exception']}"
    assert _state["result"]["summary"], "Expected non-empty summary"


@then("the entity response error is empty")
def entity_error_is_empty():
    assert _state["exception"] is None, f"tool_entity raised: {_state['exception']}"
    assert _state["result"]["error"] == "", f"Expected empty error, got {_state['result']['error']!r}"


@then(parsers.parse('the entity response error contains "{fragment}"'))
def entity_error_contains(fragment):
    assert _state["exception"] is None, f"tool_entity raised: {_state['exception']}"
    error = _state["result"].get("error", "")
    assert fragment.lower() in error.lower(), f"Expected error to contain {fragment!r}, got {error!r}"


@then("no entity exception was raised")
def no_entity_exception():
    assert _state["exception"] is None, f"tool_entity raised: {_state['exception']}"


@then("the entity response is a valid dict")
def result_is_valid_dict():
    r = _state["result"]
    assert isinstance(r, dict), f"Expected dict, got {type(r)}"
    assert "name" in r
    assert "error" in r
