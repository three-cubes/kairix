"""pytest-bdd binding for probe_config_health.feature (#provider-plugin-arch IM-9).

Steps live in ``tests/bdd/steps/probe_config_health_steps.py`` and are
registered in the root ``conftest.py``'s ``pytest_plugins`` list. The
``scenarios()`` glob below emits one pytest test per scenario in the
feature file — adding a new scenario does not require a per-scenario
binding stub here.
"""

from pathlib import Path

import pytest
from pytest_bdd import scenarios

FEATURE = str(Path(__file__).parent / "features" / "probe_config_health.feature")

pytestmark = pytest.mark.bdd

scenarios(FEATURE)
