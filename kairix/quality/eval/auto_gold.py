"""Auto gold suite generation from indexed documents.

Analyses the corpus to determine document types, then generates
evaluation queries proportioned by content type. Uses template-based
query generation (no LLM required) as the default path.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_PROCEDURAL_PATTERNS = re.compile(r"(?:^|/)(?:how-to|runbook|procedure|sop|guide|playbook|tutorial)", re.IGNORECASE)
_DATE_PATTERNS = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass
class CorpusProfile:
    """Summary of indexed corpus characteristics."""

    total_docs: int
    collections: dict[str, int]
    procedural_count: int
    date_filename_count: int
    entity_doc_count: int
    titles: list[str] = field(default_factory=list)
    # Titles whose underlying *path* matches the procedural-content shape
    # (how-to / runbook / playbook / sop). Pre-computed at analyse time
    # because path information is dropped before `generate_template_queries`
    # runs — applying _PROCEDURAL_PATTERNS to titles directly was the bug
    # fixed in #143 (procedural filter was permanently empty since titles
    # never contain path separators).
    procedural_titles: list[str] = field(default_factory=list)


def analyse_corpus(db: sqlite3.Connection) -> CorpusProfile:
    """Analyse indexed documents to determine corpus profile."""
    total = db.execute("SELECT COUNT(*) FROM documents WHERE active=1").fetchone()[0]
    collections: dict[str, int] = {}
    for row in db.execute("SELECT collection, COUNT(*) FROM documents WHERE active=1 GROUP BY collection").fetchall():
        collections[row[0]] = row[1]

    paths = db.execute("SELECT path, title FROM documents WHERE active=1").fetchall()
    procedural_paths = [(p, t) for p, t in paths if _PROCEDURAL_PATTERNS.search(p)]
    procedural = len(procedural_paths)
    date_files = sum(1 for p, _ in paths if _DATE_PATTERNS.search(p))

    # Entity docs: files in entity-like folders
    entity_pattern = re.compile(r"(?:entities|people|clients|organisations)/", re.IGNORECASE)
    entity_count = sum(1 for p, _ in paths if entity_pattern.search(p))

    titles = [t for _, t in paths if t]
    procedural_titles = [t for _, t in procedural_paths if t]

    return CorpusProfile(
        total_docs=total,
        collections=collections,
        procedural_count=procedural,
        date_filename_count=date_files,
        entity_doc_count=entity_count,
        titles=titles[:500],  # cap for memory
        procedural_titles=procedural_titles[:500],
    )


def generate_template_queries(profile: CorpusProfile, n: int = 50) -> list[dict[str, Any]]:
    """Generate evaluation queries using templates (no LLM required).

    Proportions categories based on corpus characteristics:
    - More procedural docs → more procedural queries
    - More date files → more temporal queries
    - More entity docs → more entity queries
    """
    queries: list[dict[str, Any]] = []
    titles = profile.titles[:200]

    # Calculate proportions
    total = max(profile.total_docs, 1)
    proc_ratio = min(profile.procedural_count / total, 0.3)
    date_ratio = min(profile.date_filename_count / total, 0.3)
    entity_ratio = min(profile.entity_doc_count / total, 0.2)

    # Distribute across categories
    n_recall = max(3, int(n * 0.30))
    n_conceptual = max(2, int(n * 0.15))
    n_procedural = max(2, int(n * max(0.10, proc_ratio)))
    n_temporal = max(1, int(n * max(0.05, date_ratio * 0.5)))
    n_entity = max(1, int(n * max(0.05, entity_ratio * 0.5)))
    n_multi_hop = max(1, n - n_recall - n_conceptual - n_procedural - n_temporal - n_entity)

    idx = 0
    # Recall queries — "what is X about?"
    for title in titles[:n_recall]:
        idx += 1
        readable = title.replace("-", " ").replace("_", " ")
        queries.append(
            {
                "id": f"AG-R{idx:03d}",
                "category": "recall",
                "query": f"{readable}",
                "score_method": "ndcg",
            }
        )

    # Conceptual queries — "explain the concept of X"
    for title in titles[n_recall : n_recall + n_conceptual]:
        idx += 1
        readable = title.replace("-", " ").replace("_", " ")
        queries.append(
            {
                "id": f"AG-C{idx:03d}",
                "category": "conceptual",
                "query": f"explain the concept of {readable}",
                "score_method": "ndcg",
            }
        )

    # Procedural queries — use titles pre-classified by path in analyse_corpus.
    # Applying _PROCEDURAL_PATTERNS directly to titles here used to silently
    # produce an empty list (titles never contain '/') so the fallback at
    # `proc_titles = titles[:n_procedural]` always ran, mislabelling recall
    # queries as procedural.
    proc_titles = profile.procedural_titles[:n_procedural]
    if not proc_titles:
        proc_titles = titles[:n_procedural]
    for title in proc_titles:
        idx += 1
        readable = title.replace("-", " ").replace("_", " ")
        queries.append(
            {
                "id": f"AG-P{idx:03d}",
                "category": "procedural",
                "query": f"how to {readable}",
                "score_method": "ndcg",
            }
        )

    # Temporal queries
    for i in range(n_temporal):
        idx += 1
        if i < len(titles):
            readable = titles[i].replace("-", " ").replace("_", " ")
            queries.append(
                {
                    "id": f"AG-T{idx:03d}",
                    "category": "temporal",
                    "query": f"recent changes to {readable}",
                    "score_method": "ndcg",
                }
            )

    # Entity queries
    for i in range(n_entity):
        idx += 1
        if i < len(titles):
            readable = titles[i].replace("-", " ").replace("_", " ")
            queries.append(
                {
                    "id": f"AG-E{idx:03d}",
                    "category": "entity",
                    "query": f"what is {readable}",
                    "score_method": "ndcg",
                }
            )

    # Multi-hop — combine two titles
    for i in range(n_multi_hop):
        idx += 1
        if i + 1 < len(titles):
            t1 = titles[i].replace("-", " ")
            t2 = titles[i + 1].replace("-", " ")
            queries.append(
                {
                    "id": f"AG-M{idx:03d}",
                    "category": "multi_hop",
                    "query": f"how does {t1} relate to {t2}",
                    "score_method": "ndcg",
                }
            )

    # Pad to reach target count by cycling titles with variant templates
    variant_templates = [
        ("recall", "what is {title}"),
        ("conceptual", "explain {title} in simple terms"),
        ("recall", "find documents about {title}"),
        ("conceptual", "compare {title} with alternatives"),
    ]
    vi = 0
    while len(queries) < n and titles:
        cat, tmpl = variant_templates[vi % len(variant_templates)]
        title = titles[vi % len(titles)]
        idx += 1
        readable = title.replace("-", " ").replace("_", " ")
        queries.append(
            {
                "id": f"AG-V{idx:03d}",
                "category": cat,
                "query": tmpl.format(title=readable),
                "score_method": "ndcg",
            }
        )
        vi += 1

    return queries[:n]


def build_suite(queries: list[dict[str, Any]], output_path: str) -> None:
    """Write queries as a kairix benchmark suite YAML file."""
    suite = {
        "meta": {
            "name": "auto-generated",
            "description": "Auto-generated evaluation suite from corpus analysis",
            "version": "1.0",
            "instrument": "kairix-hybrid",
            "n_cases": len(queries),
        },
        "cases": queries,
    }
    with open(output_path, "w", encoding="utf-8") as f:
        yaml.dump(suite, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    logger.info("auto_gold: wrote %d queries to %s", len(queries), output_path)
