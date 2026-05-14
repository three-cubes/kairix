"""Structured query logger — writes QueryLogEntry records to JSONL."""

from __future__ import annotations

import dataclasses
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from kairix.paths import search_log_path
from kairix.quality.eval.schema import QueryLogEntry

logger = logging.getLogger(__name__)

# Env reads (KAIRIX_SEARCH_LOG / KAIRIX_DATA_DIR) live in kairix.paths.search_log_path (F4).
_DEFAULT_LOG_PATH = str(search_log_path())


class QueryLogger:
    """
    Writes QueryLogEntry records to a JSONL file.

    Thread-safe for single-process use (append-mode open per write).
    Never raises — log failures are silently swallowed to avoid breaking search.
    """

    def __init__(self, log_path: str | Path | None = None) -> None:
        self._path = Path(log_path or _DEFAULT_LOG_PATH)

    def log(self, entry: QueryLogEntry) -> None:
        """Append entry to the log file. No-op on any error."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            row = dataclasses.asdict(entry)
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
        except Exception as exc:
            logger.debug("QueryLogger.log: failed to write — %s", exc)

    @classmethod
    def from_search_result(
        cls,
        result: object,
        agent: str,
        log_path: str | Path | None = None,
    ) -> QueryLogger:
        """
        Factory: construct a logger and immediately log a search result.

        Accepts a kairix.core.search.pipeline.SearchResult-like object.
        Returns the logger for further use.
        """
        instance = cls(log_path)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        intent = ""
        if hasattr(result, "intent"):
            intent_val = result.intent
            intent = intent_val.value if hasattr(intent_val, "value") else str(intent_val)

        top_path: str | None = None
        results_list = getattr(result, "results", [])
        if results_list:
            first = results_list[0]
            if hasattr(first, "result"):
                top_path = getattr(first.result, "path", None)

        entry = QueryLogEntry(
            ts=ts,
            agent=agent,
            query=getattr(result, "query", ""),
            intent=intent,
            result_count=len(results_list),
            bm25_count=getattr(result, "bm25_count", 0),
            vec_count=getattr(result, "vec_count", 0),
            latency_ms=getattr(result, "latency_ms", 0.0),
            top_path=top_path,
            vec_failed=getattr(result, "vec_failed", False),
            error=getattr(result, "error", None),
        )
        instance.log(entry)
        return instance
