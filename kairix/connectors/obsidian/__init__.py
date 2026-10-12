"""Obsidian connector plugin — filesystem-backed markdown knowledge stores.

First-party :class:`kairix.core.protocols.SourceConnector` implementation
for Obsidian-style markdown directories. Discovers files under a vault
root, tracks change events via :mod:`watchdog` filesystem notifications,
and periodically runs a full-scan reconciliation pass to catch events
that fired while the worker was paused. Deep-links resolve to the
``obsidian://`` URL scheme so retrieval results round-trip back into
the operator's editor.

Registered via ``[project.entry-points."kairix.connectors"]`` in
kairix's ``pyproject.toml`` — operators select it by listing
``obsidian`` in their ``connectors[]`` config. Third-party connector
plugins follow the same entry-point shape (see
``docs/architecture/connector-ingestion-architecture.md`` §8).

The plugin is mypy-strict-clean and carries ``py.typed`` per F41. The
``SourceConnector`` Protocol surface is narrow by design (F35 / F38) —
chunking, signal extraction, and Bronze persistence live in
:mod:`kairix.core.connectors`, never inside the connector itself.

See ``tests/bdd/features/connector_obsidian.feature`` for the
behaviour spec this plugin pins.
"""

from __future__ import annotations

from kairix.connectors.obsidian.connector import ObsidianConnector, make_connector
from kairix.connectors.obsidian.reconciler import FullScanReconciler
from kairix.connectors.obsidian.watcher import WatchdogSource

# F56 capability declaration. Wave B added Protocol-satisfying shims by
# duck-typing rather than inheritance; this marker lets the AST detector
# agree (the runtime isinstance probe also passes locally but CI may not
# have optional deps installed for the import probe to succeed).
# Not SlimConnector: a local walk cannot prove a prune enumeration complete,
# so Obsidian's deletions come only from reconciliation (PLA-553).
CAPABILITIES: frozenset[str] = frozenset({"SourceConnector", "PollConnector", "HierarchyConnector"})

__all__ = [
    "CAPABILITIES",
    "FullScanReconciler",
    "ObsidianConnector",
    "WatchdogSource",
    "make_connector",
]
