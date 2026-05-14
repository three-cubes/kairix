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

logger = logging.getLogger(__name__)


def _default_neo4j_client() -> Any:
    from kairix.knowledge.graph.client import get_client

    return get_client()


def _default_suggest(text: str, neo4j_client: Any) -> list[Any]:
    from kairix.knowledge.entities.suggest import suggest_entities

    return suggest_entities(text, neo4j_client)


def _default_validate(name: str, neo4j_client: Any, update: bool) -> dict[str, Any]:
    from kairix.knowledge.entities.validate import validate_entity

    return validate_entity(name, neo4j_client, update=update)


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
    """Injectable dependencies for ``run_entity_suggest``.

    Mirrors ``WorkerDeps`` (kairix/worker.py): each callable is
    non-Optional with a ``default_factory`` returning the production
    helper. Tests pass concrete fakes; production callers leave
    ``deps=None``.
    """

    suggest_fn: Callable[..., list[Any]] = field(default_factory=lambda: _default_suggest)
    neo4j_client_fn: Callable[[], Any] = field(default_factory=lambda: _default_neo4j_client)


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

    try:
        neo4j = d.neo4j_client_fn()
        raw = d.suggest_fn(text, neo4j)
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
    """Injectable dependencies for ``run_entity_validate``.

    Mirrors ``WorkerDeps``: ``validate_fn`` and ``neo4j_client_fn``
    are non-Optional with ``default_factory`` wiring the production
    helpers.
    """

    validate_fn: Callable[..., dict[str, Any]] = field(default_factory=lambda: _default_validate)
    neo4j_client_fn: Callable[[], Any] = field(default_factory=lambda: _default_neo4j_client)


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

    try:
        neo4j = d.neo4j_client_fn()
        result = d.validate_fn(name, neo4j, update=update)
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
