"""Protocols for entity suggestion pipeline.

Defines the boundary between the raw NER extractor and the post-processing
filter chain that drops role phrases, promotes known entities, and corrects
mis-typed labels.

Suggestion is the shared dict shape passed through the chain. It deliberately
uses TypedDict (rather than the existing ``SuggestedEntity`` dataclass in
``suggest.py``) because the dataclass carries Neo4j cross-reference fields
(``existing_id``, ``is_new``) that the filter chain has no business with;
keeping the shapes separate avoids coupling the filters to graph state.
"""

from __future__ import annotations

from typing import Protocol, TypedDict, runtime_checkable


class Suggestion(TypedDict, total=False):
    """A single entity suggestion flowing through the filter chain.

    Fields:
        text: The phrase NER picked up.
        label: spaCy entity label — ``"ORG"``, ``"PERSON"``, ``"GPE"``,
            ``"PRODUCT"``, ``"WORK_OF_ART"``, etc.
        source: ``"ner"`` for spaCy-extracted entries, ``"allowlist"`` for
            entries promoted by :class:`KnownEntityAllowlist`.
        confidence: Float in ``[0.0, 1.0]``. Allowlist hits get ``1.0``.
    """

    text: str
    label: str
    source: str
    confidence: float


@runtime_checkable
class SuggestionFilter(Protocol):
    """Transforms a list of entity suggestions.

    Implementations may drop, promote, or relabel entries. They must
    return a new list (not mutate the input). The filter chain is
    composed left-to-right: each filter sees the output of the previous.

    The ``context`` parameter carries the original text the suggestions
    were extracted from. It is needed by allowlist scanning (find known
    entities that NER missed) and may be used by future filters that
    inspect surrounding tokens.
    """

    def apply(self, suggestions: list[Suggestion], context: str) -> list[Suggestion]: ...
