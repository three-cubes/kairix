"""Step definitions for configurable_default_scope.feature.

Verifies that the ``in_default: bool`` flag on ``CollectionDef`` controls
default-scope membership, and that ``ConfigValidationError`` is raised
for non-boolean values. Uses real ``parse_collections`` and the real
``DefaultCollectionResolver`` — no fakes, no monkeypatch.
"""

from __future__ import annotations

import pytest
from pytest_bdd import given, parsers, then, when

from kairix.core.search.config_loader import (
    ConfigValidationError,
    parse_collections,
)
from kairix.core.search.resolver import DefaultCollectionResolver
from kairix.core.search.scope import Scope

pytestmark = pytest.mark.bdd


@pytest.fixture
def state() -> dict:
    return {"yaml_collections": [], "yaml_agents": []}


@given("a kairix.config.yaml is loaded")
def yaml_loaded(state: dict) -> None:
    state["yaml_collections"] = []
    state["yaml_agents"] = []


@given(parsers.parse('the YAML declares a collection "{name}" with no in_default field'))
def collection_without_flag(state: dict, name: str) -> None:
    state["yaml_collections"].append({"name": name, "path": name, "glob": "**/*.md"})


@given(parsers.parse('the YAML declares a collection "{name}" with in_default {flag:S}'))
def collection_with_flag(state: dict, name: str, flag: str) -> None:
    bool_value = {"true": True, "false": False}.get(flag.lower())
    if bool_value is None:
        raise ValueError(f"Unrecognised in_default flag {flag!r} in scenario data")
    state["yaml_collections"].append({"name": name, "path": name, "glob": "**/*.md", "in_default": bool_value})


@given(parsers.parse('the YAML declares a collection "{name}" with in_default value "{raw}"'))
def collection_with_raw_value(state: dict, name: str, raw: str) -> None:
    """Used for non-boolean inputs that should be rejected by the parser."""
    state["yaml_collections"].append({"name": name, "path": name, "glob": "**/*.md", "in_default": raw})


@given(parsers.parse('the YAML declares an agent "{name}"'))
def agent_declared(state: dict, name: str) -> None:
    state["yaml_agents"].append(name)


def _build_resolver(state: dict) -> DefaultCollectionResolver:
    yaml_dict = {"collections": {"shared": state["yaml_collections"]}}
    cfg = parse_collections(yaml_dict)
    return DefaultCollectionResolver(collections_config=cfg)


@when(parsers.parse("I resolve the SHARED scope for any agent"))
def resolve_shared(state: dict) -> None:
    resolver = _build_resolver(state)
    state["result"] = resolver.resolve("any-agent", Scope.SHARED) or []


@when(parsers.parse('I resolve the SHARED_AGENT scope for agent "{agent}"'))
def resolve_shared_agent(state: dict, agent: str) -> None:
    resolver = _build_resolver(state)
    state["result"] = resolver.resolve(agent, Scope.SHARED_AGENT) or []


@when(parsers.parse('I look up the collection "{name}" in the configured all-collection-names'))
def lookup_in_all_names(state: dict, name: str) -> None:
    yaml_dict = {"collections": {"shared": state["yaml_collections"]}}
    cfg = parse_collections(yaml_dict)
    state["result"] = cfg.all_collection_names() if cfg else []
    state["lookup_name"] = name


@when("I parse the collections config")
def parse_collections_step(state: dict) -> None:
    yaml_dict = {"collections": {"shared": state["yaml_collections"]}}
    try:
        parse_collections(yaml_dict)
        state["error"] = None
    except ConfigValidationError as exc:
        state["error"] = exc


@then(parsers.parse('"{name}" is included in the result'))
def result_includes(state: dict, name: str) -> None:
    assert name in state["result"], f"expected {name!r} in {state['result']!r}"


@then(parsers.parse('"{name}" is not in the result'))
def result_excludes(state: dict, name: str) -> None:
    assert name not in state["result"], f"unexpected {name!r} in {state['result']!r}"


@then(parsers.parse('"{name}" is found'))
def lookup_succeeds(state: dict, name: str) -> None:
    assert name in state["result"], f"expected {name!r} in all collection names {state['result']!r}"


@then("a ConfigValidationError is raised naming the offending key")
def validation_error_raised(state: dict) -> None:
    error = state.get("error")
    assert isinstance(error, ConfigValidationError), f"expected ConfigValidationError, got {error!r}"
    assert "in_default" in str(error), f"error message should name the offending key 'in_default'; got: {error!s}"
