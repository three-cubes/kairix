"""pytest-bdd binding for provider_chat_max_completion_tokens.feature.

Steps live in
:mod:`tests.bdd.steps.provider_chat_max_completion_tokens_steps`,
registered in the root ``conftest.py``'s ``pytest_plugins`` list.
"""

from pathlib import Path

import pytest
from pytest_bdd import scenarios

FEATURE = str(Path(__file__).parent / "features" / "provider_chat_max_completion_tokens.feature")

pytestmark = pytest.mark.bdd

scenarios(FEATURE)
