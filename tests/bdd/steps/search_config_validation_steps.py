"""Step definitions for search_config_validation.feature.

Drives kairix.core.search.config_validator.validate_config through its public surface.
No monkeypatching, no private-fn imports, no fakes needed (the validator is pure).
"""

from __future__ import annotations

import pytest
from pytest_bdd import given, then, when

from kairix.core.search.config_validator import validate_config

pytestmark = pytest.mark.bdd


# Module-scoped state — pytest-bdd executes steps for one scenario at a time.
_state: dict = {}


@given("an operator config with a retrieval override key 'rrfk' instead of 'rrf_k'")
def operator_typo_in_override():
    _state.clear()
    _state["data"] = {
        "collections": {
            "shared": [{"name": "docs", "path": "docs", "retrieval": {"rrfk": 30}}],
        }
    }


@given("an operator config whose agent_pattern omits the agent placeholder")
def operator_pattern_missing_placeholder():
    _state.clear()
    _state["data"] = {"collections": {"shared": [], "agent_pattern": "memory-bucket"}}


@given("an operator config where agent 'alpha' and agent 'beta' write into nested paths")
def operator_overlapping_write_paths():
    _state.clear()
    _state["data"] = {
        "agents": [
            {"name": "alpha", "write_path": "agents/shared"},
            {"name": "beta", "write_path": "agents/shared/notes"},
        ]
    }


@when("the operator runs config validation")
def run_validation():
    _state["errors"] = validate_config(_state["data"])


@then("the result is non-empty")
def result_non_empty():
    assert _state["errors"], "expected at least one validation error, got none"


@then("an error message names the offending key 'rrfk'")
def error_names_offending_key():
    errors = _state["errors"]
    assert any("rrfk" in e for e in errors), f"expected an error to name 'rrfk', got {errors}"


@then("the error message lists valid override keys")
def error_lists_valid_keys():
    errors = _state["errors"]
    # The validator surfaces the valid set so operators can self-correct.
    assert any("rrf_k" in e and "valid:" in e for e in errors), (
        f"expected error to enumerate valid keys (incl. rrf_k), got {errors}"
    )


@then("an error message mentions agent_pattern")
def error_mentions_agent_pattern():
    errors = _state["errors"]
    assert any("agent_pattern" in e for e in errors), f"expected agent_pattern to be named, got {errors}"


@then("the error message names the missing placeholder")
def error_names_placeholder():
    errors = _state["errors"]
    assert any("{agent}" in e for e in errors), f"expected '{{agent}}' to be named in error, got {errors}"


@then("an error message says the paths overlap")
def error_says_paths_overlap():
    errors = _state["errors"]
    assert any("overlap" in e for e in errors), f"expected overlap to be named, got {errors}"


@then("both agent names appear in the error")
def both_agent_names_in_error():
    errors = _state["errors"]
    overlap_errors = [e for e in errors if "overlap" in e]
    assert overlap_errors, f"expected an overlap error, got {errors}"
    combined = " ".join(overlap_errors)
    assert "alpha" in combined, f"expected 'alpha' in overlap error, got {overlap_errors}"
    assert "beta" in combined, f"expected 'beta' in overlap error, got {overlap_errors}"
