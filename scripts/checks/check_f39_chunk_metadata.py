"""F39: Every chunk write must carry source_uri, source_modified_at, and sensitivity.

Companion to F15 (no logging of secret-named variables) at the boundary
between connector-Silver processing and the documents/chunks store. The
schema additions in ``docs/architecture/connector-ingestion-architecture.md``
§7 add ``source_uri``, ``source_modified_at``, and ``sensitivity`` columns
to the documents/chunks store; the schema default for ``sensitivity`` is
``public``. A chunk-write callsite that omits any of the three lets a
silent default-to-public ship — confidential SharePoint content leaks
into general search.

F39 lints chunk-write constructor callsites in production code
(``kairix/**/*.py``) to verify all three kwargs appear explicitly OR
the value is read from a known config seam (e.g. the connector's
configured sensitivity tier). Default-to-public is only valid when the
caller passes it explicitly.

**The matched constructor symbol is ``Chunk``** — per
``connector-ingestion-architecture.md`` §3 the canonical value object
is ``@dataclass(frozen=True) class Chunk:`` with the three required
fields. Wave 1's scaffold lands the class; this F39 detector shape-
matches the future constructor so it fires the moment a callsite
appears that drops a required kwarg.

Today: no ``Chunk(...)`` constructor calls with these fields exist
yet. The check is **vacuous-green** — no matching callsites → no
violations. A Wave 1 commit landing the class + a non-conforming
callsite immediately triggers the rule.

Detection (AST):

1. **Constructor shape**: any ``ast.Call`` whose ``func`` resolves to
   the bare name ``Chunk`` (e.g. ``Chunk(text=..., ...)``) under
   ``kairix/**/*.py``. ``ChunkDateBoost`` and similar prefixes do NOT
   match — only the exact class name ``Chunk``.
2. **Required kwargs**: each call must pass all of ``source_uri``,
   ``source_modified_at``, and ``sensitivity`` as keyword arguments.
   ``**kwargs`` splats are conservatively accepted (the runtime
   enforcement happens at the dataclass boundary; F39 only catches
   the obvious "I forgot the kwarg" shape at the callsite).

A file appears in the violation set if any ``Chunk(...)`` call inside
it is missing one or more of the three required kwargs. There is no
grandfathering: every violation blocks at pre-commit and CI.

Allow-list: ``tests/`` is exempt because test fixtures may construct
synthetic chunks for boundary unit tests where the fields aren't
under test.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _fitness_rule import FitnessRule
from tc_fitness import python_files, repo_relative  # noqa: F401 — back-compat

# The constructor symbol the connector-ingestion ADR §3 commits to.
# Net-new connector code that builds Chunk values must populate all
# three of these kwargs explicitly. Until Wave 1 lands the class the
# detector is vacuous-green.
_CHUNK_CTOR_NAMES: frozenset[str] = frozenset({"Chunk"})

# The three kwargs every Chunk write callsite must pass. ADR §3 + §7.
_REQUIRED_KWARGS: frozenset[str] = frozenset(
    {
        "source_uri",
        "source_modified_at",
        "sensitivity",
    }
)

REMEDIATION = """F39: Chunk constructor call is missing required metadata kwargs.

Every Chunk write must populate source_uri, source_modified_at, and
sensitivity at the callsite. Schema default for sensitivity is 'public',
so an omitted kwarg silently demotes confidential content to general
search.

fix: pass source_uri=..., source_modified_at=..., sensitivity=... explicitly.
next: see connector-ingestion-architecture.md §7 (schema additions).
run: bash scripts/safe-commit.sh "fix(<area>): populate chunk metadata at write"

Pass example:
  silver_out.chunks = (
      Chunk(
          text=segment,
          content_hash=h,
          source_name=connector.name,
          source_uri=connector.source_link(item_id),
          source_modified_at=change.modified_at,
          sensitivity=connector.sensitivity_for(item_id),
          source_page=page,
      ),
  )

Forbidden example:
  Chunk(text=segment, content_hash=h, source_name="obsidian")
  # Missing source_uri / source_modified_at / sensitivity — schema
  # default 'public' would silently ship.

The legitimate sites for chunk construction are the Silver processor
in ``kairix/core/connectors/silver.py`` (Wave 1) and any future writer
that lives behind the SilverProcessor Protocol. Plugins MUST NOT
construct Chunk directly — they emit signals through the Protocol."""


def _is_chunk_ctor(call: ast.Call) -> bool:
    """True if ``call`` is a ``Chunk(...)`` constructor invocation.

    Matches a bare ``ast.Name`` whose ``id`` is in ``_CHUNK_CTOR_NAMES``.
    ``module.Chunk(...)`` (Attribute access) is intentionally NOT matched
    — F39 protects the in-package construction surface; a third-party
    plugin reaching across modules to build a Chunk by attribute access
    is already a F26/F27 violation.
    """
    return isinstance(call.func, ast.Name) and call.func.id in _CHUNK_CTOR_NAMES


def _missing_kwargs(call: ast.Call) -> set[str]:
    """Return the set of required kwargs that are NOT passed at ``call``.

    A ``**kwargs`` splat (``ast.keyword`` with ``arg is None``) is
    conservatively treated as supplying every required kwarg — F39
    only catches the obvious omission shape, not dynamic splats.
    """
    if any(kw.arg is None for kw in call.keywords):
        return set()
    passed = {kw.arg for kw in call.keywords if kw.arg is not None}
    return _REQUIRED_KWARGS - passed


def file_has_violation(path: Path) -> bool:
    """True if any ``Chunk(...)`` call in this file omits a required kwarg."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_chunk_ctor(node) and _missing_kwargs(node):
            return True
    return False


class F39(FitnessRule):
    """F39 as a FitnessRule subclass — see module docstring."""

    name = "f39"
    remediation = REMEDIATION
    roots = ("kairix",)

    def file_has_violation(self, path: Path) -> bool:
        return file_has_violation(path)


def main() -> int:
    return F39().run()


if __name__ == "__main__":
    sys.exit(main())
