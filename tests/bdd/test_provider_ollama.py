"""pytest-bdd binding for provider_ollama.feature (#provider-plugin-arch IM-11).

Steps live in:

- :mod:`tests.bdd.steps.provider_wire_common_steps` — shared
  wire-shape Given / Then phrases.
- :mod:`tests.bdd.steps.provider_ollama_steps` — Ollama-specific
  Background, When, and ``ProviderUnreachable``-with-endpoint Then
  assertions.
"""

from pathlib import Path

import pytest
from pytest_bdd import scenarios

FEATURE = str(Path(__file__).parent / "features" / "provider_ollama.feature")

pytestmark = pytest.mark.bdd

scenarios(FEATURE)
