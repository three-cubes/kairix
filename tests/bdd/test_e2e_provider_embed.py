"""pytest-bdd test module for e2e_provider_embed.feature.

Step definitions live in tests/bdd/steps/e2e_provider_embed_steps.py
and are registered via tests/conftest.py pytest_plugins so pytest-bdd
discovers them across the entire test run.
"""

from pathlib import Path

import pytest
from pytest_bdd import scenarios

# Step definitions module — imported for ``noqa: F401`` so pytest-bdd
# resolves the binding even when the steps are also registered as a
# pytest_plugin in tests/conftest.py.
from tests.bdd.steps import e2e_provider_embed_steps  # noqa: F401

FEATURE = str(Path(__file__).parent / "features" / "e2e_provider_embed.feature")

pytestmark = pytest.mark.bdd

scenarios(FEATURE)
