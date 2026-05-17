"""pytest-bdd test module for e2e_provider_health.feature.

Step definitions live in tests/bdd/steps/e2e_provider_health_steps.py
(health-specific) plus tests/bdd/steps/e2e_provider_embed_steps.py
(shared Given/Then phrases). Both are registered via
tests/conftest.py pytest_plugins.

Every When-step in the health module currently raises ``pytest.skip``
because ``kairix probe-config`` is owned by IM-9-RETRY and was not in
``origin/develop`` at this worktree's rebase point. Once IM-9-RETRY
lands, the skips inside ``e2e_provider_health_steps`` can be deleted
and the impls populated.
"""

from pathlib import Path

import pytest
from pytest_bdd import scenarios

# Step definitions modules — both imported for ``noqa: F401`` so
# pytest-bdd resolves the shared and health-specific bindings.
from tests.bdd.steps import (
    e2e_provider_embed_steps,  # noqa: F401
    e2e_provider_health_steps,  # noqa: F401
)

FEATURE = str(Path(__file__).parent / "features" / "e2e_provider_health.feature")

pytestmark = pytest.mark.bdd

scenarios(FEATURE)
