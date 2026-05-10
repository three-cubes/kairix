"""pytest-bdd test module for configurable_default_scope.feature."""

from pathlib import Path

import pytest
from pytest_bdd import scenarios

# Step definitions live alongside in steps/configurable_default_scope_steps.py — pytest-bdd
# auto-discovers them at collection time.
from tests.bdd.steps import configurable_default_scope_steps  # noqa: F401

FEATURE = str(Path(__file__).parent / "features" / "configurable_default_scope.feature")

pytestmark = pytest.mark.bdd

scenarios(FEATURE)
