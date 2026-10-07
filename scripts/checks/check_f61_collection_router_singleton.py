"""F61: CollectionRouter is the singular construction surface for chunk writers.

Every ``_SqliteChunkWriter(db, collection=...)`` construction must flow through
the framework so the per-collection routing decision stays in one place. F61
flags any bare ``_SqliteChunkWriter(...)`` construction (an ``ast.Name`` call,
not attribute-form ``mod.X(...)``) outside ``kairix/core/connectors/``.

Thin shim over :mod:`_location_engine` (#499 Phase 2). The rule is one
``LocationRule`` row in ``ctor-call`` kind; this module re-exports the
back-compat surface (``collect_violations`` / ``main`` / ``REMEDIATION``) the
F61 unit test loads by file path.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _location_engine import LocationRule, collect_violations_for, register
from tc_fitness import REPO_ROOT, gate

# Name of the writer class whose construction F61 watches.
_WRITER_CLASS_NAME = "_SqliteChunkWriter"

REMEDIATION = """F61: _SqliteChunkWriter constructor call sits outside kairix/core/connectors/.

Every chunk write must flow through CollectionRouter so the per-collection
routing logic stays in one place. Direct construction of
_SqliteChunkWriter(db, collection=...) from outside the framework fans
out the routing decision and silently bypasses any future per-collection
gating (rate-limits, dual-writes, schema overlays).

fix: replace the direct construction with a CollectionRouter lookup —
     CollectionRouter resolves (collection_name, cc_pair_id) → writer.
     Wave C lands the router; until then keep new writer construction
     under kairix/core/connectors/ only.
next: see docs/architecture/connector-scope-topology/ADR.md §"Table B"
     (CollectionRouter singleton) + 11-implementation-gap-analysis.md
     §"Critical observations" (worker.py dispatch coupling).
run: python3 scripts/checks/check_f61_collection_router_singleton.py

Pass example:
  # kairix/core/connectors/collection_router.py  — framework owns it
  class CollectionRouter:
      def writer_for(self, collection: str) -> ChunkWriter:
          if collection not in self._writers:
              self._writers[collection] = _SqliteChunkWriter(self._db, collection=collection)
          return self._writers[collection]

  # kairix/worker.py (Wave C rewire) — uses the router
  pipeline = ConnectorPipeline(
      ...,
      chunk_writer=collection_router.writer_for(name),
      ...,
  )

Forbidden example:
  # a production module outside kairix/core/connectors/
  pipeline = ConnectorPipeline(
      ...,
      chunk_writer=_SqliteChunkWriter(db, collection=name),  # F61 fires
      ...,
  )"""

RULE = register(
    LocationRule(
        name="f61",
        kind="ctor-call",
        pattern=_WRITER_CLASS_NAME,
        allowed_roots=("kairix/core/connectors",),
        remediation=REMEDIATION,
    )
)


def collect_violations(repo_root: Path = REPO_ROOT) -> set[Path]:
    """Back-compat surface for the F61 unit test."""
    return collect_violations_for(RULE, repo_root)


def main() -> int:
    """Return 0 when clean / vacuous-green; 1 on net-new violations."""
    return gate(RULE.name, collect_violations(), REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
