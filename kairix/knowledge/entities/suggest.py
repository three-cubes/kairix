"""
kairix.knowledge.entities.suggest — NER-based entity suggestion.

Uses spaCy (optional) to extract named entities from freetext input,
then runs them through a SuggestionFilter chain (drop role phrases,
promote allowlisted entities, correct mistyped labels) before
cross-referencing against the Neo4j entity graph.

Install spaCy support: pip install kairix[nlp]
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class SuggestedEntity:
    """A named entity extracted from text and cross-referenced against Neo4j."""

    text: str  # Extracted surface form
    label: str  # spaCy entity label (ORG, PERSON, GPE, etc.)
    existing_id: str | None  # Neo4j entity id if already known, else None
    existing_name: str | None  # Canonical name if already known
    is_new: bool  # True if not found in graph
    context: str = ""  # Surrounding sentence for review


_NER_LABELS_KEPT = frozenset({"ORG", "PERSON", "GPE", "PRODUCT", "WORK_OF_ART"})


def _load_nlp_or_none(nlp: Any) -> Any:
    """Return the passed-in nlp pipeline, or lazy-load en_core_web_sm.

    Returns ``None`` when spaCy is missing the model (caller treats this as
    "no NER available" and returns empty results). Raises ``ImportError``
    when spaCy itself is unavailable — that's an operator install gap.
    """
    if nlp is not None:
        return nlp
    try:
        import spacy  # lazy import — optional dependency  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "spaCy is required for entity suggestion. Install it with:\n"
            "  pip install 'kairix[nlp]'\n"
            "  python -m spacy download en_core_web_sm"
        ) from exc
    try:
        return _load_model()
    except Exception as exc:
        logger.warning("suggest_entities: spaCy load failed — %s", exc)
        return None


def _extract_ner_suggestions(doc: Any) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """First pass — collect unique surface forms + their context sentences."""
    ner_suggestions: list[dict[str, Any]] = []
    context_map: dict[str, str] = {}
    for sent in doc.sents:
        for ent in sent.ents:
            if ent.label_ not in _NER_LABELS_KEPT:
                continue
            key = ent.text.strip()
            if not key or key in context_map:
                continue
            context_map[key] = sent.text.strip()[:200]
            ner_suggestions.append(
                {
                    "text": key,
                    "label": ent.label_,
                    "source": "ner",
                    "confidence": 1.0,
                }
            )
    return ner_suggestions, context_map


def _resolve_filter_chain(filter_chain: Any) -> Any:
    """Return the caller's chain, or build the default one."""
    if filter_chain is not None:
        return filter_chain
    from kairix.knowledge.entities.filters import default_suggestion_filter_chain

    return default_suggestion_filter_chain()


def _lookup_existing_entity(
    neo4j_client: Any, surface_form: str, input_text: str
) -> tuple[str | None, str | None, bool]:
    """Return ``(existing_id, existing_name, is_new)`` from a Neo4j find-by-name lookup."""
    try:
        rows = neo4j_client.find_by_name(surface_form)
    except Exception as exc:
        logger.debug("suggest_entities: Neo4j lookup for %r failed — %s", surface_form, exc)
        return (None, None, True)
    phantom_filtered = _filter_phantom_rows(rows, surface_form=surface_form, input_text=input_text)
    if not phantom_filtered:
        return (None, None, True)
    return (
        str(phantom_filtered[0].get("id", "")),
        str(phantom_filtered[0].get("name", "")),
        False,
    )


def suggest_entities(
    text: str,
    neo4j_client: Any,
    *,
    filter_chain: Any = None,
    nlp: Any = None,
) -> list[SuggestedEntity]:
    """Extract named entities from text and cross-reference against Neo4j.

    Args:
        text: Freetext input (document body, meeting notes, etc.)
        neo4j_client: Neo4jClient instance (kairix.knowledge.graph.client.get_client()).
            When unavailable, returns [] with a warning.
        filter_chain: Optional SuggestionFilter applied to NER hits before Neo4j
            lookup. When None, uses default_suggestion_filter_chain() — drops
            role phrases, no allowlist, no label overrides. Production callers
            constructing a chain at the boundary (factory.py) pass it in here
            so the suggester gets per-deployment allowlists and overrides.
        nlp: Optional spaCy nlp pipeline. When None, lazily loads en_core_web_sm
            via _load_model. Tests pass a fake to bypass the spaCy import path.

    Returns:
        List of SuggestedEntity, deduped by surface form, after filter chain.
        Never raises.
    """
    if not getattr(neo4j_client, "available", False):
        logger.warning("suggest_entities: Neo4j unavailable — returning empty list")
        return []

    nlp = _load_nlp_or_none(nlp)
    if nlp is None:
        return []

    try:
        doc = nlp(text)
    except Exception as exc:
        logger.warning("suggest_entities: spaCy processing failed — %s", exc)
        return []

    ner_suggestions, context_map = _extract_ner_suggestions(doc)
    filter_chain = _resolve_filter_chain(filter_chain)
    filtered = filter_chain.apply(ner_suggestions, context=text)

    results: list[SuggestedEntity] = []
    for suggestion in filtered:
        surface_form = suggestion.get("text", "")
        if not surface_form:
            continue
        existing_id, existing_name, is_new = _lookup_existing_entity(neo4j_client, surface_form, text)
        results.append(
            SuggestedEntity(
                text=surface_form,
                label=suggestion.get("label", ""),
                existing_id=existing_id,
                existing_name=existing_name,
                is_new=is_new,
                context=context_map.get(surface_form, ""),
            )
        )

    return results


def _load_model() -> Any:
    """Load en_core_web_sm model. Raises RuntimeError with install instructions if missing."""
    import spacy

    try:
        return spacy.load("en_core_web_sm")
    except OSError as exc:
        raise RuntimeError(
            "spaCy model 'en_core_web_sm' not found. Install it with:\n  python -m spacy download en_core_web_sm"
        ) from exc


def _filter_phantom_rows(
    rows: list[dict[str, Any]],
    *,
    surface_form: str,
    input_text: str,
) -> list[dict[str, Any]]:
    """Drop phantom existing-entity hits returned by Neo4j fuzzy match.

    ``Neo4jClient.find_by_name`` uses ``CONTAINS`` semantics so a token
    like ``"brown"`` matches a stored entity ``"Brown Corp"`` even when
    the input doesn't reference the full canonical name. The planner
    (BM25 fuzzy expansion) relies on that behaviour, so the fix lives
    here, post-lookup, instead of in the graph client.

    A row is carried forward only when one of:

    1. The stored ``name`` equals the ``surface_form`` (case-insensitive).
       NER pulled out the canonical name as a unit — clearly a real hit.
    2. The stored ``name`` appears as a word-boundary token in the
       original input text. The CONTAINS expansion picked a fragment but
       the full name is genuinely present.

    Everything else is a phantom: the graph returned a hit because of
    substring fuzziness, not because the entity is actually mentioned.
    """
    if not rows:
        return []
    surface_lower = surface_form.lower()
    kept: list[dict[str, Any]] = []
    for row in rows:
        name = str(row.get("name", "")).strip()
        if not name:
            continue
        if name.lower() == surface_lower:
            kept.append(row)
            continue
        if _name_appears_in_text(name, input_text):
            kept.append(row)
    return kept


def _name_appears_in_text(name: str, text: str) -> bool:
    """Return True when ``name`` appears as a word-boundary token in ``text``.

    Mirrors the word-boundary semantics used by
    :class:`kairix.knowledge.entities.filters.KnownEntityAllowlist` so
    both halves of #249's phantom-hit defence apply the same precision
    floor.
    """
    if not name:
        return False
    import re

    escaped = re.escape(name)
    left = r"\b" if name[0].isalnum() or name[0] == "_" else r"(?:^|(?<=\W))"
    right = r"\b" if name[-1].isalnum() or name[-1] == "_" else r"(?:$|(?=\W))"
    return bool(re.search(rf"{left}{escaped}{right}", text, re.IGNORECASE))


def format_suggestions(suggestions: list[SuggestedEntity], fmt: str = "table") -> str:
    """Format suggestions as a table or JSONL string."""
    if not suggestions:
        return "No entity suggestions found.\n"

    if fmt == "jsonl":
        import dataclasses
        import json

        return "\n".join(json.dumps(dataclasses.asdict(s)) for s in suggestions) + "\n"

    lines = [
        f"{'ENTITY':<35} {'TYPE':<10} {'STATUS':<10} CONTEXT",
        "-" * 100,
    ]
    for s in suggestions:
        status = "existing" if not s.is_new else "NEW"
        name = s.existing_name or s.text
        lines.append(f"{name:<35} {s.label:<10} {status:<10} {s.context[:40]!r}")
    return "\n".join(lines) + "\n"
