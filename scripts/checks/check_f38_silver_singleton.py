"""F38: Silver processing only in ``kairix/core/connectors/silver.py``.

Silver is the work that turns a Bronze record into ``(chunks,
entity_signals)`` — chunking + entity-signal extraction. F38 keeps every
chunking primitive (``def`` names matching ``chunk_*`` / ``_chunk*`` /
``tokenize_into_chunks``) in one canonical home, with the existing
conversational-corpus chunkers permitted by the rule's declared legacy roots.

Thin shim over :mod:`_location_engine` (#499 Phase 2). The rule is one
``LocationRule`` row in ``def-name`` kind; this module re-exports the
back-compat surface (``collect_violations`` / ``main`` / ``REMEDIATION`` /
``_is_chunk_function_name``) the F38 unit test loads by file path.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _location_engine import LocationRule, collect_violations_for, register
from tc_fitness import REPO_ROOT, gate

# Regex matching a chunking function name. Anchored at both ends.
_CHUNK_NAME_RE = re.compile(
    r"""
    ^(
        chunk_[a-z0-9_]+        # chunk_text, chunk_board, chunk_file
        | _chunk                # bare _chunk private helper
        | _chunk_[a-z0-9_]+     # _chunk_date_boost_impl, _chunk_to_hit
        | tokenize_into_chunks
    )$
    """,
    re.VERBOSE,
)

REMEDIATION = """Refactor to extract the chunking logic into
kairix/core/connectors/silver.py and call it from your code. Silver is
the single processing surface that takes a Bronze record and returns
(chunks, entity_signals). Per-connector chunkers re-create the format
sprawl the architecture exists to remove — every new source would grow
its own splitter, its own signal extractor, its own off-by-one bug.

fix: extract chunking into kairix/core/connectors/silver.py; call it from your code.
     The connector reads bytes and yields ChangeEvents; the extractor turns bytes
     into an ExtractedDocument; the orchestrator (kairix/core/connectors/pipeline.py)
     calls Silver to produce (chunks, entity_signals).
next: replace the inline chunker with a call into kairix.core.connectors.silver, or
     re-import the existing surface (chunk_text / chunk_file / chunk_board) from the
     conversational-corpus path if the use is orthogonal.
run: python3 scripts/checks/check_f38_silver_singleton.py to confirm green.

Pass example:
  kairix/core/connectors/silver.py             # canonical Silver — allowed
  kairix/core/temporal/chunker.py              # existing conversational corpus — allowed
  kairix/core/embed/embed.py (chunk_text)      # embed-time chunker — allowed
  kairix/quality/probe/chunk_perf_fixture.py   # perf fixture — allowed

Forbidden example:
  kairix/connectors/sharepoint/chunker.py      # F38 — per-connector chunker
  kairix/extractors/markitdown/chunk_page.py   # F38 — per-extractor chunker
  kairix/core/connectors/pipeline.py (chunk_*) # F38 — chunker outside silver.py

Why: see docs/architecture/connector-ingestion-architecture.md §5.1 +
§6. One Silver surface keeps the Bronze→Silver transition uniform across
every source: same chunker, same signal extractor, same boundary, same
defects pinned by tests. Per-connector chunkers fork the surface and
the discipline rots."""

RULE = register(
    LocationRule(
        name="f38",
        kind="def-name",
        pattern=_CHUNK_NAME_RE,
        allowed_roots=(
            "kairix/quality/probe",
            "kairix/corpus",
            "kairix/core/temporal",
            "kairix/core/embed",
            "kairix/core/search",
            "kairix/use_cases",
        ),
        allowed_files=("kairix/core/connectors/silver.py",),
        remediation=REMEDIATION,
    )
)


def _is_chunk_function_name(name: str) -> bool:
    """True if ``name`` matches the chunking-function naming pattern.
    Re-exported for the F38 test."""
    return _CHUNK_NAME_RE.match(name) is not None


def collect_violations(repo_root: Path = REPO_ROOT) -> set[Path]:
    """Back-compat surface for the F38 unit test."""
    return collect_violations_for(RULE, repo_root)


def main() -> int:
    return gate(RULE.name, collect_violations(), REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
