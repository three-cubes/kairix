"""Entity use cases — entity suggest + entity validate.

Phase 3b of the CLI/MCP feature parity initiative (#168). Pre-Phase-3b
both operations were CLI-only; agents needed to shell out to extract
entities from prose or validate them against Wikidata. This module
absorbs the per-operation logic into use cases returning uniform
dataclasses; both adapters serialise from them.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from kairix.use_cases import _entity_defaults as _defaults

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# entity_suggest
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SuggestedEntityHit:
    """A single NER suggestion projected from ``SuggestedEntity``."""

    text: str
    label: str
    is_new: bool
    existing_id: str = ""
    existing_name: str = ""
    context: str = ""


@dataclass(frozen=True)
class EntitySuggestOutput:
    text: str
    suggestions: list[SuggestedEntityHit] = field(default_factory=list)
    new_count: int = 0
    existing_count: int = 0
    error: str = ""


@dataclass(frozen=True)
class EntitySuggestDeps:
    suggest_fn: Callable[..., list[Any]] | None = None
    neo4j_client_fn: Callable[[], Any] | None = None


def _project_suggestion(s: Any) -> SuggestedEntityHit:
    return SuggestedEntityHit(
        text=str(getattr(s, "text", "")),
        label=str(getattr(s, "label", "")),
        is_new=bool(getattr(s, "is_new", False)),
        existing_id=str(getattr(s, "existing_id", "") or ""),
        existing_name=str(getattr(s, "existing_name", "") or ""),
        context=str(getattr(s, "context", "")),
    )


def run_entity_suggest(
    text: str,
    *,
    deps: EntitySuggestDeps | None = None,
) -> EntitySuggestOutput:
    """Run NER over ``text`` and cross-reference with Neo4j.

    Never raises — failures populate ``error``.
    """
    d = deps or EntitySuggestDeps()
    suggest = d.suggest_fn or _defaults.default_suggest
    neo4j_factory = d.neo4j_client_fn or _defaults.default_neo4j_client

    try:
        neo4j = neo4j_factory()
        raw = suggest(text, neo4j)
        hits = [_project_suggestion(s) for s in raw]
        new_count = sum(1 for h in hits if h.is_new)
        return EntitySuggestOutput(
            text=text,
            suggestions=hits,
            new_count=new_count,
            existing_count=len(hits) - new_count,
        )
    except ImportError as exc:
        # spaCy missing — operator-actionable.
        return EntitySuggestOutput(
            text=text,
            error=f"ImportError: {exc}. Install with: pip install 'kairix[nlp]'",
        )
    except Exception as exc:
        logger.warning("run_entity_suggest failed: %s", exc, exc_info=True)
        return EntitySuggestOutput(text=text, error=f"{type(exc).__name__}: {exc}")


def entity_suggest_output_to_envelope(out: EntitySuggestOutput) -> dict[str, Any]:
    return {
        "text": out.text,
        "suggestions": [
            {
                "text": h.text,
                "label": h.label,
                "is_new": h.is_new,
                "existing_id": h.existing_id,
                "existing_name": h.existing_name,
                "context": h.context,
            }
            for h in out.suggestions
        ],
        "new_count": out.new_count,
        "existing_count": out.existing_count,
        "error": out.error,
    }


# ---------------------------------------------------------------------------
# entity_validate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EntityValidateMatch:
    qid: str
    label: str
    description: str
    url: str
    confidence: str  # high | medium | low


@dataclass(frozen=True)
class EntityValidateOutput:
    name: str
    neo4j_id: str = ""
    matches: list[EntityValidateMatch] = field(default_factory=list)
    updated: bool = False
    error: str = ""


@dataclass(frozen=True)
class EntityValidateDeps:
    validate_fn: Callable[..., dict[str, Any]] | None = None
    neo4j_client_fn: Callable[[], Any] | None = None


def _project_match(m: Any) -> EntityValidateMatch:
    if isinstance(m, dict):
        return EntityValidateMatch(
            qid=str(m.get("qid", "")),
            label=str(m.get("label", "")),
            description=str(m.get("description", "")),
            url=str(m.get("url", "")),
            confidence=str(m.get("confidence", "")),
        )
    return EntityValidateMatch(
        qid=str(getattr(m, "qid", "")),
        label=str(getattr(m, "label", "")),
        description=str(getattr(m, "description", "")),
        url=str(getattr(m, "url", "")),
        confidence=str(getattr(m, "confidence", "")),
    )


def run_entity_validate(
    name: str,
    *,
    update: bool = False,
    deps: EntityValidateDeps | None = None,
) -> EntityValidateOutput:
    """Validate ``name`` against Wikidata and optionally update Neo4j.

    Never raises — failures populate ``error``.
    """
    d = deps or EntityValidateDeps()
    validate = d.validate_fn or _defaults.default_validate
    neo4j_factory = d.neo4j_client_fn or _defaults.default_neo4j_client

    try:
        neo4j = neo4j_factory()
        result = validate(name, neo4j, update=update)
        matches = [_project_match(m) for m in result.get("matches", [])]
        return EntityValidateOutput(
            name=str(result.get("name", name)),
            neo4j_id=str(result.get("neo4j_id") or ""),
            matches=matches,
            updated=bool(result.get("updated", False)),
            error=str(result.get("error", "") or ""),
        )
    except Exception as exc:
        logger.warning("run_entity_validate failed: %s", exc, exc_info=True)
        return EntityValidateOutput(name=name, error=f"{type(exc).__name__}: {exc}")


def entity_validate_output_to_envelope(out: EntityValidateOutput) -> dict[str, Any]:
    return {
        "name": out.name,
        "neo4j_id": out.neo4j_id,
        "matches": [
            {
                "qid": m.qid,
                "label": m.label,
                "description": m.description,
                "url": m.url,
                "confidence": m.confidence,
            }
            for m in out.matches
        ],
        "updated": out.updated,
        "error": out.error,
    }
