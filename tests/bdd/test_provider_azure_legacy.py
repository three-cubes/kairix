"""pytest-bdd binding for provider_azure_legacy.feature (#provider-plugin-arch IM-7 Wave-4 skeleton).

The ``azure_legacy`` plugin is a Wave-4 NotImplementedError stub. Steps
live in :mod:`tests.bdd.steps.provider_azure_legacy_steps` and dispatch
``pytest.skip("plugin not implemented yet — Wave 4")`` from inside
each step body so collection is green and F28 (every plugin has a
matching feature) sees behavioural coverage scaffolding.
"""

from pathlib import Path

import pytest
from pytest_bdd import scenarios

FEATURE = str(Path(__file__).parent / "features" / "provider_azure_legacy.feature")

pytestmark = pytest.mark.bdd

scenarios(FEATURE)
