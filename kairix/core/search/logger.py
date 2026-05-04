"""JSONL-backed structured logger for search and query events.

Implements the SearchLogger Protocol (kairix.core.protocols.SearchLogger).

The logger appends one JSON object per event to a configurable path,
creating parent directories on demand. The path is provided at
construction time (G4: config at boundary — no env-var reads here).

Production wiring resolves the path in factory.py:
  - Docker: /data/kairix/logs/search.jsonl
  - Non-Docker: ~/.cache/kairix/logs/search.jsonl

Event schema (additions vs. the existing ad-hoc log):
  - "agent": str | None — calling agent name, if known
  - "scope": str | None — scope value (one of: shared, agent, shared+agent, all-agents, everything)
  - "collections_searched": list[str] | None — actual collections passed to backends
  - "vec_failed": bool — whether vector search failed for this query

Existing fields preserved: ts (ISO 8601 UTC), query_hash, intent,
bm25_count, vec_count, fused_count, total_tokens, latency_ms,
result_count, success.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class JsonlSearchLogger:
    """Append-only JSONL logger satisfying the SearchLogger Protocol.

    Construction takes explicit paths — the logger never reads environment
    variables or config files (G4: config at boundary). Path resolution is
    the caller's responsibility (see ``default_search_log_paths`` for the
    canonical path-computation helper, and ``factory.py`` for the actual
    base-directory decision in production).

    Thread safety: append-only writes are atomic on the line level when each
    event is written via a single ``f.write(line)`` call; no file-level locks
    are required for typical CLI/MCP usage.

    Failure mode: write failures (disk full, permission error, path is a
    directory, etc.) are caught and emitted as a single WARNING log line via
    ``logging.getLogger(__name__)`` and otherwise dropped. Search must not
    break because logging broke.
    """

    def __init__(
        self,
        *,
        search_log_path: Path,
        query_log_path: Path | None = None,
    ) -> None:
        self._search_log_path = search_log_path
        self._query_log_path = query_log_path

    def log_search(self, event: dict[str, Any]) -> None:
        """Append one JSON line to the search log path.

        Augments the event with an ISO 8601 UTC ``ts`` field if absent.
        Creates parent directories on first call. Never raises — write
        failures are caught and logged at WARNING level and otherwise
        dropped. (Search must not break because logging broke.)
        """
        self._append(self._search_log_path, event)

    def log_query(self, event: dict[str, Any]) -> None:
        """Append one JSON line to the query log path, if configured.

        No-op when ``query_log_path`` is None (privacy-gated; matches the
        existing behaviour where raw query logging is opt-in). Never raises —
        write failures are caught and logged at WARNING level and otherwise
        dropped. (Search must not break because logging broke.)
        """
        if self._query_log_path is None:
            return
        self._append(self._query_log_path, event)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _append(self, path: Path, event: dict[str, Any]) -> None:
        """Write a single JSON line to ``path``, augmenting ``ts`` if missing.

        Never raises — any OSError or value-encoding error is caught and
        emitted as a single WARNING log line and otherwise dropped.
        (Search must not break because logging broke.)
        """
        # Augment with ISO 8601 UTC timestamp if not already set.
        if "ts" not in event:
            event = {**event, "ts": datetime.now(timezone.utc).isoformat()}

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(event) + "\n"
            with path.open("a", encoding="utf-8") as f:
                f.write(line)
        except (OSError, TypeError, ValueError) as e:
            # Never raise — search must not break because logging broke.
            logger.warning("JsonlSearchLogger: failed to write event to %s — %s", path, e)


def default_search_log_paths(*, base: Path | None = None) -> tuple[Path, Path]:
    """Return canonical ``(search_log_path, query_log_path)`` for ``base``.

    Pure path-computation utility — does not read environment variables or
    config files. The decision of which base directory to use lives in
    ``factory.py`` (production: ``/data/kairix/logs`` under Docker, or
    ``~/.cache/kairix/logs`` otherwise).

    When ``base`` is ``None``, defaults to ``/data/kairix/logs`` — the
    Docker production path. Callers who want the non-Docker default
    must pass it explicitly.
    """
    resolved_base = base if base is not None else Path("/data/kairix/logs")
    return (resolved_base / "search.jsonl", resolved_base / "query.jsonl")
