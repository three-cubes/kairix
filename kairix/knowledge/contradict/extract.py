"""Claim extraction for the contradict pipeline.

Replaces the historical 500-char truncation in detector.py: short claims
fit, longer claims discard their second half. The dogfood report
surfaced this in cases where the load-bearing assertion is mid-paragraph
or a longer multi-claim statement.

EntityDensityClaimExtractor splits the input into sentences and ranks
them by entity density (proper-noun count) and modal-verb presence
(claims with "is", "must", "always", "never", "only", etc. are more
contradiction-relevant than narrative). Returns the top-N as separate
search queries; the detector runs the full search per claim and unions
candidates.
"""

from __future__ import annotations

import re

# Words that strengthen a sentence's status as a contradiction-relevant claim.
_MODAL_WORDS = re.compile(
    r"\b(is|are|was|were|will|must|cannot|can't|never|always|only|exclusive|"
    r"monopoly|the\s+(?:first|sole|unique)|published|shipped|closed|active|"
    r"superseded|deprecated)\b",
    re.IGNORECASE,
)

# Tokens we treat as "entity-shaped" — capitalised words longer than 2 chars
# (skipping the common sentence-initial article).
_PROPER_NOUN = re.compile(r"\b[A-Z][A-Za-z][A-Za-z0-9]{1,}\b")

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")


class EntityDensityClaimExtractor:
    """Rank sentences by entity-density + modal-verb presence; return top-N."""

    def extract(self, content: str, *, top_n: int = 3) -> list[str]:
        if not content or not content.strip():
            return []

        # Split on sentence boundaries; coarse but adequate.
        sentences = [s.strip() for s in _SENT_SPLIT.split(content.strip()) if s.strip()]
        if not sentences:
            return [content[:500]]  # whole content treated as one claim

        scored: list[tuple[float, str]] = []
        for sent in sentences:
            entity_count = len(_PROPER_NOUN.findall(sent))
            modal_count = len(_MODAL_WORDS.findall(sent))
            length = max(1, len(sent.split()))
            # Density = entities per word, with modal verbs weighted in.
            score = (entity_count + 0.6 * modal_count) / max(length, 8)
            scored.append((score, sent))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [sent for _, sent in scored[:top_n]]
