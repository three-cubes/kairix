"""pytest-bdd test module for e2e_provider_chat.feature.

Step definitions live in tests/bdd/steps/e2e_provider_chat_steps.py
(chat-specific) plus tests/bdd/steps/e2e_provider_embed_steps.py
(shared Given/Then phrases). Both are registered via
tests/conftest.py pytest_plugins.
"""

from pathlib import Path

import pytest
from pytest_bdd import scenarios

# Step definitions modules — both imported for ``noqa: F401`` so
# pytest-bdd resolves the shared and chat-specific bindings.
from tests.bdd.steps import (
    e2e_provider_chat_steps,  # noqa: F401
    e2e_provider_embed_steps,  # noqa: F401
)

FEATURE = str(Path(__file__).parent / "features" / "e2e_provider_chat.feature")

pytestmark = pytest.mark.bdd

scenarios(FEATURE)
