"""Contract tests for :class:`kairix.agents.mcp.text_mode_composers.TextModeComposer`.

PR 2.8 / #421 introduces the composer registry that maps CLI
subcommands to ``(from_envelope, format_text)`` pairs. The dispatcher
uses the registry to render text mode from a warm-MCP envelope without
re-running the in-process pipeline.

Contract surface:

* ``TextModeComposer`` is a frozen dataclass with three fields:
  ``from_envelope`` (callable taking a dict, returning a result object),
  ``format_text`` (callable taking ``(result, argv)`` and returning a
  string), and ``name`` (str for diagnostics).
* ``register_composer(subcommand, composer)`` adds an entry; calling it
  twice with the same subcommand replaces the prior entry (last-write
  wins so import-order regressions don't cause silent stale renders).
* ``get_composer(subcommand)`` returns the entry or ``None`` for
  unknown subcommands.
* ``list_registered()`` returns the registered subcommand names sorted
  so diagnostic output is stable.

The registry-function behaviour (``register_composer`` / ``get_composer``
/ ``list_registered`` and the import-leaf rule) is pinned in
``tests/unit/test_text_mode_composer_registry.py``; this file keeps the
shape contract, run over a probe AND every production-registered composer.

F1/F2-clean: tests construct the dataclass directly + drive the public
register/get/list surface; no monkeypatch on kairix internals.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import FrozenInstanceError

import pytest

from kairix.agents.mcp.client_dispatcher import ensure_composers_loaded
from kairix.agents.mcp.text_mode_composers import (
    TextModeComposer,
    get_composer,
)

pytestmark = pytest.mark.contract


_PROBE = TextModeComposer(
    from_envelope=lambda env: dict(env),
    format_text=lambda result, argv: f"rendered:{result}:{argv}",
    name="example-subcmd",
)


def _registered(subcommand: str) -> Callable[[], tuple[str, TextModeComposer | None]]:
    """Resolve a production composer through the dispatcher's canonical wiring."""

    def _resolve() -> tuple[str, TextModeComposer | None]:
        ensure_composers_loaded()
        return subcommand, get_composer(subcommand)

    return _resolve


# Sabotage-proof (executed): mutated ``TextModeComposer`` to add a
# second mandatory field; this test failed with TypeError missing
# argument. Restored. Sabotage-proof for the production legs: changed
# the ``"timeline"`` composer's ``name=`` in the canonical wiring to
# ``"timeline-x"``; the ``timeline`` leg failed. Restored.
@pytest.mark.parametrize(
    "subject",
    [
        lambda: ("example-subcmd", _PROBE),
        *(
            _registered(sub)
            for sub in ("bootstrap", "brief", "caches", "contradict", "prep", "research", "search", "timeline")
        ),
    ],
    ids=["probe", "bootstrap", "brief", "caches", "contradict", "prep", "research", "search", "timeline"],
)
def test_text_mode_composer_is_frozen_dataclass_with_three_fields(
    subject: Callable[[], tuple[str, TextModeComposer | None]],
) -> None:
    """The dataclass shape: from_envelope + format_text + name — for a
    test-constructed composer AND every production-registered composer
    (whose ``name`` must match the subcommand it is registered under)."""
    subcommand, composer = subject()
    assert isinstance(composer, TextModeComposer)
    assert composer.name == subcommand
    assert callable(composer.from_envelope)
    assert callable(composer.format_text)
    # Frozen — assignment must raise FrozenInstanceError (stdlib dataclasses)
    with pytest.raises(FrozenInstanceError):
        composer.name = "mutated"  # type: ignore[misc]  # F3 rationale: frozen-write probe
