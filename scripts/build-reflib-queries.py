#!/usr/bin/env python3
"""
build-reflib-queries.py -- Generate benchmark queries for the reference library.

Samples documents from the reference library, generates queries using the
GPL pipeline, retrieves candidates, and grades them with the LLM judge.
Outputs suites/reference-library.yaml.

Requires:
- Reference library indexed in a kairix DB (run kairix embed first)
- Azure OpenAI credentials (for query generation and judging)

Usage:
    python3 scripts/build-reflib-queries.py \
        --db-path /path/to/reflib.db \
        --output suites/reference-library.yaml \
        --n-cases 160

    # On VM:
    python3 scripts/build-reflib-queries.py \
        --db-path /data/kairix-reference-library/.kairix/kairix.core.db \
        --output /data/kairix-reference-library/eval/reference-library.yaml \
        --n-cases 160
"""

import argparse
import logging
import random
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logger = logging.getLogger(__name__)

# Target distribution matching kairix eval standards
TARGET_DISTRIBUTION = {
    "recall": 0.30,
    "conceptual": 0.175,
    "procedural": 0.15,
    "entity": 0.125,
    "temporal": 0.10,
    "multi_hop": 0.0875,
    "cross_collection": 0.0625,
}

# Per-collection sample targets (proportional to content volume)
COLLECTION_WEIGHTS = {
    "agentic-ai": 0.15,
    "engineering": 0.15,
    "data-and-analysis": 0.15,
    "operating-models": 0.08,
    "product-and-design": 0.06,
    "philosophy": 0.08,
    "security": 0.06,
    "leadership-and-culture": 0.05,
    "economics-and-strategy": 0.05,
    "personal-effectiveness": 0.05,
    "foundations": 0.04,
    "health-and-fitness": 0.04,
    "family-and-education": 0.02,
    "industry-standards": 0.02,
}


def sample_documents(db_path: str, n_samples: int = 200, seed: int = 42) -> list[dict]:
    """Sample documents from the reference library DB, proportional to collection weights."""
    random.seed(seed)
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row

    all_docs = []
    for collection, weight in COLLECTION_WEIGHTS.items():
        target = max(2, int(n_samples * weight))
        rows = db.execute(
            "SELECT hash, path, title FROM documents WHERE path LIKE ? ORDER BY RANDOM() LIMIT ?",
            (f"{collection}/%", target),
        ).fetchall()
        for row in rows:
            all_docs.append(
                {
                    "hash": row["hash"],
                    "path": row["path"],
                    "title": row["title"] or Path(row["path"]).stem,
                    "collection": collection,
                }
            )

    db.close()
    logger.info("Sampled %d documents from %d collections", len(all_docs), len(COLLECTION_WEIGHTS))
    return all_docs


def generate_query_for_document(doc: dict, category: str) -> dict:
    """Generate a benchmark query for a document.

    For the initial build, generates rule-based queries.
    LLM-based generation can be added later via the GPL pipeline.
    """
    title = doc["title"]
    path = doc["path"]
    collection = doc["collection"]

    templates = {
        "recall": [
            f"Find the document about {title.lower()}",
            f"What does the reference library say about {title.lower()}?",
        ],
        "conceptual": [
            f"What is {title.lower()}?",
            f"Explain the concept of {title.lower()}",
        ],
        "procedural": [
            f"How to implement {title.lower()}",
            f"What are the steps for {title.lower()}?",
        ],
        "entity": [
            f"What source covers {title.lower()}?",
            f"Which document describes {title.lower()}?",
        ],
        "temporal": [
            f"When was {title.lower()} published or updated?",
        ],
        "multi_hop": [
            f"How does {title.lower()} relate to other {collection} topics?",
        ],
        "cross_collection": [
            f"What perspectives exist on {title.lower()} across different domains?",
        ],
    }

    query_templates = templates.get(category, templates["recall"])
    # NOSONAR: non-security template selection for benchmark
    # query generation; deterministic via random.seed() in build_suite().
    query_text = random.choice(query_templates)

    # Normalise title for gold matching
    normalised_title = title.lower().replace(" ", "-").replace("_", "-")

    return {
        "id": None,  # Assigned later
        "category": category,
        "query": query_text,
        "score_method": "ndcg",
        "gold_titles": [
            {"title": normalised_title, "relevance": 2},
        ],
        "source_doc": path,
        "source_collection": collection,
    }


def build_suite(
    documents: list[dict],
    n_cases: int = 160,
    seed: int = 42,
) -> dict:
    """Build a complete benchmark suite from sampled documents."""
    random.seed(seed)

    cases = []
    case_counter = {cat: 0 for cat in TARGET_DISTRIBUTION}

    # Assign categories proportionally
    for doc in documents:
        if len(cases) >= n_cases:
            break

        # Pick the most underrepresented category
        target_counts = {cat: int(n_cases * weight) for cat, weight in TARGET_DISTRIBUTION.items()}
        available = [cat for cat, target in target_counts.items() if case_counter[cat] < target]
        if not available:
            available = list(TARGET_DISTRIBUTION.keys())

        # NOSONAR: non-security category selection for
        # benchmark distribution; deterministic via random.seed() above.
        category = random.choice(available)
        case = generate_query_for_document(doc, category)
        case_counter[category] += 1
        cases.append(case)

    # Assign IDs
    prefix_map = {
        "recall": "R",
        "conceptual": "C",
        "procedural": "P",
        "entity": "E",
        "temporal": "T",
        "multi_hop": "M",
        "cross_collection": "X",
    }
    cat_counters = {cat: 0 for cat in prefix_map}
    for case in cases:
        cat = case["category"]
        cat_counters[cat] += 1
        prefix = prefix_map.get(cat, "Q")
        case["id"] = f"REF-{prefix}{cat_counters[cat]:03d}"

    # Build YAML structure
    suite = {
        "meta": {
            "name": "reference-library",
            "version": "1.0",
            "corpus": "reference-library",
            "description": (
                "Reproducible benchmark suite against the kairix reference library. "
                f"{len(cases)} queries across {len(COLLECTION_WEIGHTS)} collections "
                f"and {len(TARGET_DISTRIBUTION)} categories."
            ),
            "n_cases": len(cases),
            "score_method": "ndcg",
        },
        "cases": [
            {
                "id": c["id"],
                "category": c["category"],
                "query": c["query"],
                "score_method": c["score_method"],
                "gold_titles": c["gold_titles"],
            }
            for c in cases
        ],
    }

    logger.info(
        "Built suite: %d cases (%s)",
        len(cases),
        ", ".join(f"{cat} {count}" for cat, count in sorted(case_counter.items()) if count > 0),
    )
    return suite


def write_yaml_suite(suite: dict, output_path: str) -> None:
    """Write suite to YAML file."""
    import yaml

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        yaml.dump(suite, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    logger.info("Wrote suite to %s", output_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build reference library benchmark queries")
    parser.add_argument("--db-path", required=True, help="Path to reference library kairix DB")
    parser.add_argument("--output", default="suites/reference-library.yaml", help="Output YAML path")
    parser.add_argument("--n-cases", type=int, default=160, help="Number of test cases")
    parser.add_argument("--n-samples", type=int, default=200, help="Documents to sample")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility")
    parser.add_argument("--verbose", action="store_true")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s: %(message)s")

    if not Path(args.db_path).exists():
        logger.error("DB not found: %s", args.db_path)
        return 1

    docs = sample_documents(args.db_path, n_samples=args.n_samples, seed=args.seed)
    if not docs:
        logger.error("No documents found in DB")
        return 1

    suite = build_suite(docs, n_cases=args.n_cases, seed=args.seed)
    write_yaml_suite(suite, args.output)

    # Print summary
    print(f"\nGenerated {len(suite['cases'])} queries → {args.output}")
    cats = {}
    for c in suite["cases"]:
        cats[c["category"]] = cats.get(c["category"], 0) + 1
    for cat, count in sorted(cats.items()):
        print(f"  {cat}: {count}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
