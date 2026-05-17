"""pytest-bdd binding for provider_azure_foundry.feature (#provider-plugin-arch IM-7).

Steps live in:

- :mod:`tests.bdd.steps.provider_wire_common_steps` — shared
  wire-shape Given / Then phrases.
- :mod:`tests.bdd.steps.provider_azure_foundry_steps` — Foundry-
  specific Background, When, and provider-name-bearing typed-error
  assertions.

Both modules are registered in the root ``conftest.py``'s
``pytest_plugins`` list.
"""

from pathlib import Path

import pytest
from pytest_bdd import scenarios

FEATURE = str(Path(__file__).parent / "features" / "provider_azure_foundry.feature")

pytestmark = pytest.mark.bdd

scenarios(FEATURE)
