"""pytest-bdd test module for wikilinks_injection.feature."""

from pathlib import Path

import pytest
from pytest_bdd import scenarios

from tests.bdd.steps import wikilinks_injection_steps  # noqa: F401

FEATURE = str(Path(__file__).parent / "features" / "wikilinks_injection.feature")

pytestmark = pytest.mark.bdd

scenarios(FEATURE)
