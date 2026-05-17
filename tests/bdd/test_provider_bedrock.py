"""pytest-bdd binding for provider_bedrock.feature (#provider-plugin-arch IM-7 Wave-4 skeleton).

Steps live in :mod:`tests.bdd.steps.provider_bedrock_steps` and skip
with the Wave-4 rationale; see that module's docstring.
"""

from pathlib import Path

import pytest
from pytest_bdd import scenarios

FEATURE = str(Path(__file__).parent / "features" / "provider_bedrock.feature")

pytestmark = pytest.mark.bdd

scenarios(FEATURE)
