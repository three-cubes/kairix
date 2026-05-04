"""Typed scope for multi-agent retrieval.

Replaces the historical Literal["shared", "agent", "shared+agent"] typing
across search, prep, brief, and timeline. Adds two new scope values for
the cross-agent memory architecture: ALL_AGENTS and EVERYTHING.

Scope subclasses str so existing string-equality comparisons keep working
during migration. New code should use Scope.SHARED_AGENT etc.
"""

from __future__ import annotations

from enum import Enum


class Scope(str, Enum):
    """Typed scope for multi-agent retrieval.

    Subclassing str preserves string-equality with the historical
    Literal["shared", "agent", "shared+agent"] values, so call sites
    can migrate incrementally without breaking comparisons.
    """

    SHARED = "shared"  # shared collections only
    AGENT = "agent"  # this agent's collection only
    SHARED_AGENT = "shared+agent"  # default — current behavior
    ALL_AGENTS = "all-agents"  # all agent collections, no shared (KFEAT-GAP-8)
    EVERYTHING = "everything"  # shared + all agents — cross-team synthesis

    @classmethod
    def parse(cls, value: str | Scope) -> Scope:
        """Parse a string or Scope into a Scope, raising on unknown values."""
        if isinstance(value, cls):
            return value
        for member in cls:
            if member.value == value:
                return member
        raise ValueError(f"unknown scope {value!r}; valid: {[m.value for m in cls]}")
