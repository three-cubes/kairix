"""pytest-bdd binding for entity_audit.feature (#260, #261)."""

from __future__ import annotations

import pytest
from pytest_bdd import scenario


@pytest.mark.bdd
@scenario("features/entity_audit.feature", "audit emits a JSON report containing the documented shape")
def test_audit_emits_json_shape() -> None:
    """Body populated by @scenario from the .feature file."""


@pytest.mark.bdd
@scenario("features/entity_audit.feature", "purge dry-run reads the audit report and previews the deletes")
def test_purge_dry_run_previews() -> None:
    """Body populated by @scenario from the .feature file."""
