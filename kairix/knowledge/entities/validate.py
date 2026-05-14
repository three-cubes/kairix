"""
kairix.knowledge.entities.validate — Wikidata entity validator.

Looks up an entity name against the Wikidata public API and returns a
candidate QID + canonical label. Optionally writes wikidata_qid back to
the Neo4j node property with --update.

No API key required (Wikidata public REST API).
Timeout: 10 seconds. Never raises — returns empty result on any failure.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import requests

logger = logging.getLogger(__name__)

WIKIDATA_SEARCH_URL = "https://www.wikidata.org/w/api.php"
_DEFAULT_TIMEOUT = 10


@dataclass
class WikidataMatch:
    """Result of a Wikidata entity search."""

    qid: str  # Wikidata item ID (e.g. "Q123456")
    label: str  # Canonical English label
    description: str  # Short description
    url: str  # Wikidata item URL
    confidence: str  # "high" | "medium" | "low" based on label match


def search_wikidata(
    name: str,
    language: str = "en",
    http_get: Callable[..., requests.Response] | None = None,
) -> list[WikidataMatch]:
    """
    Search Wikidata for entities matching name.

    Args:
        name: Entity name to search for.
        language: Language code for labels and descriptions.
        http_get: Injectable HTTP GET function for testing.
                  Defaults to ``requests.get``.

    Returns:
        List of WikidataMatch (up to 5), ordered by Wikidata relevance.
        Returns [] on any network error or API failure. Never raises.
    """
    if http_get is None:
        http_get = requests.get

    params: dict[str, str | int] = {
        "action": "wbsearchentities",
        "search": name,
        "language": language,
        "format": "json",
        "limit": 5,
        "type": "item",
    }
    try:
        resp = http_get(
            WIKIDATA_SEARCH_URL,
            params=params,
            timeout=_DEFAULT_TIMEOUT,
            headers={"User-Agent": "kairix-entity-validator/0.9 (https://github.com/quanyeomans/kairix)"},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("search_wikidata(%r): %s", name, exc)
        return []

    results = []
    name_lower = name.lower()
    for item in data.get("search", []):
        qid = str(item.get("id", ""))
        label = str(item.get("label", ""))
        description = str(item.get("description", ""))
        url = f"https://www.wikidata.org/wiki/{qid}"

        # Confidence based on label similarity
        label_lower = label.lower()
        if label_lower == name_lower:
            confidence = "high"
        elif name_lower in label_lower or label_lower in name_lower:
            confidence = "medium"
        else:
            confidence = "low"

        results.append(
            WikidataMatch(
                qid=qid,
                label=label,
                description=description,
                url=url,
                confidence=confidence,
            )
        )

    return results


def validate_entity(
    name: str,
    neo4j_client: object,
    update: bool = False,
    http_get: Callable[..., requests.Response] | None = None,
) -> dict[str, Any]:
    """
    Validate an entity against Wikidata and optionally update the Neo4j node.

    Args:
        name: Entity name to look up.
        neo4j_client: Neo4jClient instance.
        update: If True and a high-confidence match is found, write wikidata_qid
                to the Neo4j node.

    Returns:
        dict with keys: name, neo4j_id, matches (list), updated, error.
        Never raises.
    """
    # Look up entity in Neo4j
    neo4j_id = None
    try:
        rows = neo4j_client.find_by_name(name)  # type: ignore[attr-defined]  # neo4j_client typed as object; duck-typed and exception-guarded
        if rows:
            neo4j_id = str(rows[0].get("id", ""))
    except Exception as exc:
        logger.warning("validate_entity: Neo4j lookup failed — %s", exc)

    # Search Wikidata
    matches = search_wikidata(name, http_get=http_get)

    updated = False
    if update and neo4j_id and matches:
        best = matches[0]
        if best.confidence in ("high", "medium"):
            try:
                neo4j_client.cypher(  # type: ignore[attr-defined]  # neo4j_client typed as object; duck-typed and exception-guarded
                    "MATCH (n {id: $id}) SET n.wikidata_qid = $qid",
                    {"id": neo4j_id, "qid": best.qid},
                )
                updated = True
                logger.info(
                    "validate_entity: set wikidata_qid=%s on node %s",
                    best.qid,
                    neo4j_id,
                )
            except Exception as exc:
                logger.warning("validate_entity: Neo4j update failed — %s", exc)

    return {
        "name": name,
        "neo4j_id": neo4j_id,
        "matches": [
            {
                "qid": m.qid,
                "label": m.label,
                "description": m.description,
                "url": m.url,
                "confidence": m.confidence,
            }
            for m in matches
        ],
        "updated": updated,
        "error": "",
    }
