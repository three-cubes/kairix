"""Contract test for the Scope enum.

This is the contract the rest of the codebase relies on during the
Literal -> Scope migration: Scope must be a str-subclassed Enum and
expose exactly the canonical value set.
"""

from __future__ import annotations

from enum import Enum

import pytest

from kairix.core.search.scope import Scope


@pytest.mark.contract
def test_scope_is_str_enum_with_canonical_values() -> None:
    assert issubclass(Scope, str)
    assert issubclass(Scope, Enum)
    assert {member.value for member in Scope} == {
        "shared",
        "agent",
        "shared+agent",
        "all-agents",
        "everything",
    }
