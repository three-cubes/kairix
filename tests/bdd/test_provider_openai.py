"""pytest-bdd binding for provider_openai.feature (#provider-plugin-arch IM-7).

Steps live in:

- :mod:`tests.bdd.steps.provider_wire_common_steps` — shared
  wire-shape Given / Then phrases.
- :mod:`tests.bdd.steps.provider_openai_steps` — OpenAI-specific
  Background, When, and provider-name-bearing typed-error assertions.
"""

from pathlib import Path

import pytest
from pytest_bdd import scenarios

FEATURE = str(Path(__file__).parent / "features" / "provider_openai.feature")

pytestmark = pytest.mark.bdd

scenarios(FEATURE)
