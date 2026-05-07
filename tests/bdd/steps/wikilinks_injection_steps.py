"""Step definitions for wikilinks_injection.feature.

Exercises the production wikilinks injector and eligibility predicate
through the new ``paths: KairixPaths`` injection seam introduced in the
paths-DI refactor (Phase 0). No env-var monkeypatching, no fakes beyond
``FakePaths`` — production code is invoked directly with sentinel paths.
"""

from __future__ import annotations

import pytest
from pytest_bdd import given, parsers, then, when

from kairix.knowledge.wikilinks.injector import inject_wikilinks, should_inject
from kairix.knowledge.wikilinks.resolver import WikiEntity
from kairix.paths import KairixPaths
from tests.fakes import FakePaths

pytestmark = pytest.mark.bdd


@pytest.fixture
def state() -> dict:
    return {
        "entities": [],
        "content": "",
        "source_path": "",
        "modified": "",
        "injected": [],
        "path_under_test": "",
        "eligible": None,
    }


# ---------------------------------------------------------------------------
# Background
# ---------------------------------------------------------------------------


@given("a KairixPaths is constructed with sentinel test roots")
def kairixpaths_constructed(state: dict) -> None:
    state["paths"] = FakePaths(
        document_root="/var/lib/kairix-test/vault",
        workspace_root="/var/lib/kairix-test/workspaces",
    )


# ---------------------------------------------------------------------------
# Entity / content setup
# ---------------------------------------------------------------------------


@given(parsers.parse('an entity "{name}" with link "{link}"'))
def given_entity(state: dict, name: str, link: str) -> None:
    state["entities"].append(WikiEntity(name=name, aliases=[], vault_path="", link=link, entity_type="organisation"))


@given(parsers.parse('an entity "{name}" with link "{link}" and vault path "{vault_path}"'))
def given_entity_with_path(state: dict, name: str, link: str, vault_path: str) -> None:
    state["entities"].append(
        WikiEntity(name=name, aliases=[], vault_path=vault_path, link=link, entity_type="organisation")
    )


@given(parsers.parse('a markdown body "{content}"'))
def given_body(state: dict, content: str) -> None:
    state["content"] = content


@given("the source path is the entity's own overview page")
def source_is_own_page(state: dict) -> None:
    paths: KairixPaths = state["paths"]
    state["source_path"] = f"{paths.document_root}/02-Areas/Clients/Acme-Corp/Overview.md"


# ---------------------------------------------------------------------------
# Path eligibility setup (path strings constructed against the test paths)
# ---------------------------------------------------------------------------


@given(parsers.parse('a markdown path under the document_root at "{rel}"'))
def path_under_doc_root(state: dict, rel: str) -> None:
    paths: KairixPaths = state["paths"]
    state["path_under_test"] = f"{paths.document_root}/{rel}"


@given(parsers.parse('a markdown path under the workspace_root at "{rel}"'))
def path_under_workspace(state: dict, rel: str) -> None:
    paths: KairixPaths = state["paths"]
    state["path_under_test"] = f"{paths.workspace_root}/{rel}"


@given(parsers.parse('a markdown path "{abs_path}"'))
def given_absolute_path(state: dict, abs_path: str) -> None:
    state["path_under_test"] = abs_path


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


@when("I inject wikilinks")
def when_inject(state: dict) -> None:
    state["modified"], state["injected"] = inject_wikilinks(
        state["content"],
        state["entities"],
        source_path=state["source_path"],
        paths=state["paths"],
    )


@when("I check injection eligibility")
def when_check_eligibility(state: dict) -> None:
    state["eligible"] = should_inject(state["path_under_test"], paths=state["paths"])


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------


@then(parsers.parse('the result contains "{snippet}" exactly once'))
def result_contains_once(state: dict, snippet: str) -> None:
    assert state["modified"].count(snippet) == 1, f"expected exactly one {snippet!r} in {state['modified']!r}"


@then(parsers.parse('the result contains no "{snippet}"'))
def result_contains_none(state: dict, snippet: str) -> None:
    assert snippet not in state["modified"], f"unexpected {snippet!r} in {state['modified']!r}"


@then(parsers.parse('"{name}" appears in the injected list'))
def injected_includes(state: dict, name: str) -> None:
    assert name in state["injected"], f"expected {name!r} in {state['injected']!r}"


@then(parsers.parse('"{name}" does not appear in the injected list'))
def injected_excludes(state: dict, name: str) -> None:
    assert name not in state["injected"], f"unexpected {name!r} in {state['injected']!r}"


@then("the file is eligible")
def is_eligible(state: dict) -> None:
    assert state["eligible"] is True, f"expected eligible for {state['path_under_test']!r}"


@then("the file is not eligible")
def is_not_eligible(state: dict) -> None:
    assert state["eligible"] is False, f"expected not eligible for {state['path_under_test']!r}"
