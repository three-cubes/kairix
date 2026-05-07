"""pytest-bdd test module for agent_collections.feature (#115)."""

from pathlib import Path

import pytest
from pytest_bdd import scenarios

# Step definitions live alongside in steps/agent_collections_steps.py — pytest-bdd
# auto-discovers them at collection time.
from tests.bdd.steps import agent_collections_steps  # noqa: F401

FEATURE = str(Path(__file__).parent / "features" / "agent_collections.feature")

pytestmark = pytest.mark.bdd

scenarios(FEATURE)
