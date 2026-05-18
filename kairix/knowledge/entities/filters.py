"""Strategy implementations of :class:`SuggestionFilter`.

Composing these filters in order via :class:`ChainedSuggestionFilter` produces
the entity-suggest correction pipeline that fixes the dogfood-reported bug
where role phrases (e.g., "the regional team") leaked through as ``ORG`` and
short proper-noun acronyms the small NER model doesn't recognise (e.g.,
"MIT") were silently dropped.

Each filter is a self-contained Strategy: no if/elif dispatch on type,
no module-level globals, all configuration injected at construction.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from kairix.knowledge.entities.protocols import Suggestion, SuggestionFilter


@dataclass
class OverrideMatchCounter:
    """Per-override match counter shared between filter and crawl orchestrator.

    Closes #263. The :class:`KnownEntityAllowlist` records one increment
    per override-text that matches an input context; the crawl orchestrator
    reads the counts at end-of-crawl to write the coverage report. This
    object is the DI seam — passed into the filter at construction so
    the filter has nowhere else to write to (no module-level globals,
    no thread-locals — F1/F2 clean).

    ``counts`` maps override text to its match count. ``record(text)``
    increments by one; absent keys default to zero. The crawl writer
    intersects ``counts`` keys with the known allowlist to derive the
    never-matched set.
    """

    counts: Counter[str] = field(default_factory=Counter)

    def record(self, text: str) -> None:
        """Increment the match count for ``text`` by one."""
        if text:
            self.counts[text] += 1

    def get(self, text: str) -> int:
        """Return the recorded match count for ``text`` (0 if absent)."""
        return int(self.counts.get(text, 0))


# ---------------------------------------------------------------------------
# Role-phrase filter — drops job titles / role descriptors
# ---------------------------------------------------------------------------


class RolePhraseFilter:
    """Drops suggestions whose ``text`` matches a role-phrase pattern.

    Three patterns are checked, all against the suggestion's ``text``
    only (the surrounding ``context`` is ignored here):

    1. ``the [Word]+`` — leading definite article followed by one or more
       words, e.g. "the regional lead", "the platform team".
    2. ``[Title] (Officer|Director|Lead|Manager|VP|Head|Chief|President|
       Engineer|Architect)`` — a single capitalised word followed by a
       known role noun. Plain role titles without a name attached.
    3. All-lowercase short phrases (1-3 words) tagged as ``ORG`` —
       almost always a role descriptor, never a real organisation name.

    Patterns are compiled once at construction.
    """

    # Words after the article must be alphabetic; allows uppercase acronyms.
    # IGNORECASE is set, so character classes use `[a-z]` only — adding `A-Z`
    # would duplicate every range under IGNORECASE (Sonar python:S5869).
    _ARTICLE_PATTERN = re.compile(r"^the\s+[a-z]+(?:\s+[a-z]+)+$", re.IGNORECASE)

    # Plain-role: capitalised word + role noun (no further words — that would
    # imply a person's full title, e.g. "John Smith Director" is unusual; we
    # treat exact two-word title-role pairs as plain role titles).
    _PLAIN_ROLE_PATTERN = re.compile(
        r"^[A-Z][a-z]+\s+(Officer|Director|Lead|Manager|VP|Head|Chief|President|Engineer|Architect)$"
    )

    def apply(self, suggestions: list[Suggestion], context: str) -> list[Suggestion]:
        """Return a new list with role-phrase entries removed."""
        del context  # unused — we only inspect each suggestion's text
        return [s for s in suggestions if not self._is_role_phrase(s)]

    def _is_role_phrase(self, suggestion: Suggestion) -> bool:
        text = suggestion.get("text", "")
        if not text:
            return False
        if self._ARTICLE_PATTERN.match(text):
            return True
        if self._PLAIN_ROLE_PATTERN.match(text):
            return True
        return self._is_lowercase_org_phrase(text, suggestion.get("label", ""))

    @staticmethod
    def _is_lowercase_org_phrase(text: str, label: str) -> bool:
        """All-lowercase ORG suggestions of 1-3 words are role descriptors."""
        if label != "ORG":
            return False
        if not text.islower():
            return False
        word_count = len(text.split())
        return 1 <= word_count <= 3


# ---------------------------------------------------------------------------
# Known-entity allowlist — promotes entities the NER model missed
# ---------------------------------------------------------------------------


class KnownEntityAllowlist:
    """Promotes pre-loaded known entities that NER missed.

    The allowlist is supplied as a list of :class:`Suggestion` dicts at
    construction time (G4: configuration at the boundary — file resolution
    happens in the factory, not here). For each allowlist entry whose
    ``text`` appears as a **word-boundary token** in the ``context``
    (case-insensitive) and is missing from the input suggestions, a new
    entry is appended with ``source="allowlist"`` and ``confidence=1.0``.
    Existing suggestions are preserved unchanged, even if their text
    matches an allowlist entry.

    Match semantics (#249 fix). Substring matching (``text in context``)
    fired false positives — an override for ``"BB"`` matched ``"abbey"``
    or ``"BBBB"``. Word-boundary matching via ``re.search(r"\\b{text}\\b",
    context, IGNORECASE)`` requires the term to be a standalone token. The
    ``case_insensitive`` flag at the override-file layer still works:
    expansion at load time produces lower/upper/title variants, each of
    which the word-boundary regex matches case-insensitively here.
    """

    def __init__(
        self,
        entities: list[Suggestion],
        *,
        match_counter: OverrideMatchCounter | None = None,
    ) -> None:
        self._entities: list[Suggestion] = list(entities)
        # DI seam for #263: when a counter is injected, every word-boundary
        # match against ``context`` records an increment for the override
        # text. Production crawl-time wiring passes a shared counter the
        # orchestrator later reads to write the coverage report. Tests
        # pass their own counter to assert on increments. Default None
        # keeps existing suggest-pipeline callers unchanged.
        self._match_counter: OverrideMatchCounter | None = match_counter

    def apply(self, suggestions: list[Suggestion], context: str) -> list[Suggestion]:
        result: list[Suggestion] = list(suggestions)
        existing_texts = {s.get("text", "").lower() for s in suggestions}
        for entry in self._entities:
            text = entry.get("text", "")
            if not text:
                continue
            if not _matches_word_boundary(text, context):
                continue
            # Record the match before dedup — overrides that match get
            # counted even when an earlier NER hit already covered the
            # same surface form. The coverage report is "did this override
            # ever fire," not "did it produce a unique promotion."
            if self._match_counter is not None:
                self._match_counter.record(text)
            if text.lower() in existing_texts:
                continue
            promoted: Suggestion = {
                "text": text,
                "label": entry.get("label", ""),
                "source": "allowlist",
                "confidence": 1.0,
            }
            result.append(promoted)
            existing_texts.add(text.lower())
        return result


def _matches_word_boundary(text: str, context: str) -> bool:
    """Return True when ``text`` appears as a word-boundary token in ``context``.

    Case-insensitive. ``re.escape`` neutralises regex metacharacters so
    override entries containing punctuation (``"C++"``, ``"AT&T"``) don't
    blow up the regex compiler. Each end of the pattern uses ``\\b`` only
    when the adjacent character is a word character — Python's ``\\b``
    asserts the transition between a word and a non-word character, so
    anchoring against a non-word character at the boundary (e.g. trailing
    ``+`` in ``"C++"``) would never match. Falling back to a lookaround
    against ``\\W`` or string-boundary keeps the punctuation case working.
    """
    if not text:
        return False
    escaped = re.escape(text)
    left_boundary = r"\b" if text[0].isalnum() or text[0] == "_" else r"(?:^|(?<=\W))"
    right_boundary = r"\b" if text[-1].isalnum() or text[-1] == "_" else r"(?:$|(?=\W))"
    pattern = re.compile(rf"{left_boundary}{escaped}{right_boundary}", re.IGNORECASE)
    return bool(pattern.search(context))


# ---------------------------------------------------------------------------
# NER label filter — corrects known mis-types
# ---------------------------------------------------------------------------


class NerLabelFilter:
    """Relabels known mis-types using injected override sets.

    This is the rule-based correction layer for entities the NER model
    consistently mis-labels. ``person_overrides`` forces ``label="PERSON"``
    for matching ``text``; ``org_overrides`` forces ``label="ORG"``. Other
    suggestions pass through unchanged.

    The filter does not consult the ``source`` field — both NER and
    allowlist entries are subject to the same correction rules.
    """

    def __init__(self, person_overrides: set[str], org_overrides: set[str]) -> None:
        self._person_overrides: set[str] = set(person_overrides)
        self._org_overrides: set[str] = set(org_overrides)

    def apply(self, suggestions: list[Suggestion], context: str) -> list[Suggestion]:
        del context  # unused — overrides are looked up by text alone
        return [self._relabel(s) for s in suggestions]

    def _relabel(self, suggestion: Suggestion) -> Suggestion:
        text = suggestion.get("text", "")
        new_label = self._lookup_override(text)
        if new_label is None:
            # Pass through — return a shallow copy to avoid aliasing.
            return dict(suggestion)  # type: ignore[return-value]  # dict() loses TypedDict narrowing; runtime matches Suggestion
        updated: Suggestion = dict(suggestion)  # type: ignore[assignment]  # dict() loses TypedDict narrowing; runtime matches Suggestion
        updated["label"] = new_label
        return updated

    def _lookup_override(self, text: str) -> str | None:
        if text in self._person_overrides:
            return "PERSON"
        if text in self._org_overrides:
            return "ORG"
        return None


# ---------------------------------------------------------------------------
# Chained filter — composes a list of filters left-to-right
# ---------------------------------------------------------------------------


class ChainedSuggestionFilter:
    """Composes a list of :class:`SuggestionFilter` left-to-right.

    Each filter receives the output of the previous. An empty chain is a
    pass-through.
    """

    def __init__(self, filters: list[SuggestionFilter]) -> None:
        self._filters: list[SuggestionFilter] = list(filters)

    def apply(self, suggestions: list[Suggestion], context: str) -> list[Suggestion]:
        current: list[Suggestion] = list(suggestions)
        for filt in self._filters:
            current = filt.apply(current, context)
        return current


# ---------------------------------------------------------------------------
# Default factory — composes the standard correction chain
# ---------------------------------------------------------------------------


def default_suggestion_filter_chain(
    *,
    allowlist: list[Suggestion] | None = None,
    person_overrides: set[str] | None = None,
    org_overrides: set[str] | None = None,
) -> ChainedSuggestionFilter:
    """Build the default entity-suggest correction chain.

    Order is significant:

    1. :class:`RolePhraseFilter` — drop role descriptors first (cheap).
    2. :class:`KnownEntityAllowlist` — promote known entities NER missed.
    3. :class:`NerLabelFilter` — apply rule-based label corrections last.

    All keyword arguments default to empty containers so callers can opt
    into individual layers without ceremony.
    """
    return ChainedSuggestionFilter(
        filters=[
            RolePhraseFilter(),
            KnownEntityAllowlist(allowlist or []),
            NerLabelFilter(person_overrides or set(), org_overrides or set()),
        ]
    )
