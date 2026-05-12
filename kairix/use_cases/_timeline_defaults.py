"""Production wiring for ``run_timeline``.

The use case itself takes a ``TimelineDeps`` bag of callables (every
field optional). When a field is None, the use case calls the matching
``default_*`` here to lazy-import + wire the production component.

Why this lives in a separate module: keeps ``kairix/use_cases/timeline.py``
import-light (no heavy ``temporal.index`` / ``core.factory`` imports at
module load), and gives unit tests a 100%-coverable target since they
inject deps explicitly and never touch this module.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from kairix.core.search.scope import Scope


def default_extract_window(query: str, reference_date: date | None) -> tuple[date | None, date | None]:
    from kairix.core.temporal.rewriter import extract_time_window

    return extract_time_window(query=query, reference_date=reference_date)


def default_rewrite_query(query: str, reference_date: date | None) -> str:
    from kairix.core.temporal.rewriter import rewrite_temporal_query

    rewritten = rewrite_temporal_query(query=query, reference_date=reference_date)
    return rewritten if rewritten is not None else query


def default_query_chunks(
    topic: str,
    start: date | None,
    end: date | None,
    chunk_types: list[str] | None,
    limit: int,
) -> list[Any]:
    from kairix.core.temporal.index import query_temporal_chunks

    return query_temporal_chunks(
        topic=topic,
        start=start,
        end=end,
        chunk_types=chunk_types,
        limit=limit,
    )


def default_search(
    query: str,
    budget: int,
    agent: str | None,
    scope: Scope,
) -> Any:
    from kairix.core.factory import build_search_pipeline

    pipeline = build_search_pipeline()
    return pipeline.search(query=query, budget=budget, agent=agent, scope=scope)
