"""kairix provider plugin layer.

Per-endpoint plugins live under ``kairix/providers/<name>/`` and
register via the ``kairix.providers`` Python entry-point group in
their distribution's ``pyproject.toml``. Core code never imports a
concrete provider — only the ``Provider`` Protocol defined here.

See ``docs/architecture/provider-plugin-architecture.md`` for the
architectural decision record that defines this split (three-layer
architecture: ``core/`` ← ``transport/`` ← ``providers/``) and the
F26-F29 fitness functions that enforce it.

Public surface:

- ``Provider`` — Protocol every plugin satisfies (embed_batch, chat,
  dimension, healthcheck).
- ``ProviderRegistry`` — Protocol that resolves a configured name to
  a ``Provider`` instance.
- ``ProviderHealth`` — dataclass returned by ``Provider.healthcheck``.
- ``ProviderNotRegistered`` — typed exception with name + available
  list when an operator selects an unknown plugin.
- ``get_provider(name, registry=None)`` — convenience accessor; defaults
  to a fresh ``EntryPointRegistry``. Tests pass a ``FakeProviderRegistry``.
"""

from __future__ import annotations

from kairix.providers._base import (
    ENTRY_POINT_GROUP,
    EntryPointRegistry,
    Provider,
    ProviderHealth,
    ProviderNotRegistered,
    ProviderRegistry,
    get_provider,
)

__all__ = [
    "ENTRY_POINT_GROUP",
    "EntryPointRegistry",
    "Provider",
    "ProviderHealth",
    "ProviderNotRegistered",
    "ProviderRegistry",
    "get_provider",
]
