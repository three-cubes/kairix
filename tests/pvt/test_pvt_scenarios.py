"""Bind every PVT feature file to the pytest collection.

One ``scenarios()`` call per feature file auto-binds all scenarios in
that file. Each generated test carries ``pytest.mark.pvt`` so the
``conftest.py`` autoskip catches them when ``KAIRIX_PVT`` is unset.

When #284 ships the harness, the placeholder steps in
``tests/pvt/steps/pvt_placeholder_steps.py`` get replaced with real
bodies and these scenario bindings start executing.
"""

from pathlib import Path

import pytest
from pytest_bdd import scenarios

_FEATURES = Path(__file__).parent / "features"

pytestmark = pytest.mark.pvt

scenarios(str(_FEATURES / "agent_cold_start_experience.feature"))
scenarios(str(_FEATURES / "agent_warm_baseline.feature"))
scenarios(str(_FEATURES / "teaming_load_experience.feature"))
scenarios(str(_FEATURES / "session_start_storm.feature"))
scenarios(str(_FEATURES / "in_session_stability.feature"))
