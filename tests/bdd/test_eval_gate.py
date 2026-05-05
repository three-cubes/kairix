"""pytest-bdd test runner for eval_gate.feature (KFEAT-013, stage 5)."""

from pathlib import Path

import pytest
from pytest_bdd import scenarios

FEATURE = str(Path(__file__).parent / "features" / "eval_gate.feature")

pytestmark = pytest.mark.bdd

scenarios(FEATURE)
