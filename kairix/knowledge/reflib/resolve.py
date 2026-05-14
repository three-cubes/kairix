"""Entity resolution — dedup, merge aliases, and fuzzy-match raw entities.

Takes the raw entity list from ``extract.py`` and produces a resolved,
deduplicated list ready for Neo4j stub emission.  Uses Levenshtein
similarity (>=0.85 within the same type) for fuzzy matching.  No external
NLP libraries — just string distance.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from kairix.knowledge.reflib.extract import RawEntity
from kairix.utils import slugify

# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass
class ResolvedEntity:
    """A deduplicated, canonical entity ready for graph loading."""

    id: str  # slug
    canonical_name: str
    entity_type: str
    description: str = ""
    domains: list[str] = field(default_factory=list)
    source_docs: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    confidence: float = 1.0


# ---------------------------------------------------------------------------
# Slug generation
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Levenshtein distance (pure Python, no external deps)
# ---------------------------------------------------------------------------


def _levenshtein(a: str, b: str) -> int:
    """Compute Levenshtein edit distance between two strings."""
    if len(a) < len(b):
        return _levenshtein(b, a)
    if not b:
        return len(a)

    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            cost = 0 if ca == cb else 1
            curr.append(min(curr[j] + 1, prev[j + 1] + 1, prev[j] + cost))
        prev = curr
    return prev[-1]


def _similarity(a: str, b: str) -> float:
    """Normalised similarity between two strings (0.0-1.0)."""
    if a == b:
        return 1.0
    max_len = max(len(a), len(b))
    if max_len == 0:
        return 1.0
    return 1.0 - _levenshtein(a, b) / max_len


# ---------------------------------------------------------------------------
# Merge helpers
# ---------------------------------------------------------------------------


def _merge_lists(*lists: list[str]) -> list[str]:
    """Merge multiple lists, preserving order and removing duplicates."""
    seen: set[str] = set()
    result: list[str] = []
    for lst in lists:
        for item in lst:
            if item and item not in seen:
                seen.add(item)
                result.append(item)
    return result


def _pick_canonical(names: list[str]) -> str:
    """Pick the best canonical name from a list of candidates.

    Prefers: longest non-acronym name, then most common.
    """
    if not names:
        return ""
    # Count occurrences
    counts: dict[str, int] = defaultdict(int)
    for n in names:
        counts[n] += 1

    # Sort by: not-all-upper first (prefer expanded names), then longest, then most frequent
    candidates = sorted(
        counts.keys(),
        key=lambda n: (not n.isupper(), len(n), counts[n]),
        reverse=True,
    )
    return candidates[0]


# ---------------------------------------------------------------------------
# Resolution pipeline — extracted steps
# ---------------------------------------------------------------------------


def group_by_slug_and_type(
    raw: list[RawEntity],
) -> dict[tuple[str, str], list[RawEntity]]:
    """Group raw entities by (slug, entity_type) key."""
    groups: dict[tuple[str, str], list[RawEntity]] = defaultdict(list)
    for entity in raw:
        slug = slugify(entity.name)
        if not slug:
            continue
        groups[(slug, entity.entity_type)].append(entity)
    return groups


def merge_within_groups(
    groups: dict[tuple[str, str], list[RawEntity]],
) -> dict[tuple[str, str], ResolvedEntity]:
    """Merge aliases, descriptions, and domains within each (slug, type) group."""
    merged: dict[tuple[str, str], ResolvedEntity] = {}
    for (slug, etype), members in groups.items():
        names = [m.name for m in members]
        canonical = _pick_canonical(names)
        all_aliases = _merge_lists(*[m.aliases for m in members])
        for n in names:
            if n != canonical and n not in all_aliases:
                all_aliases.append(n)

        all_domains = _merge_lists(*[m.domains for m in members])
        all_docs = _merge_lists(*[m.source_docs for m in members])
        best_conf = max(m.confidence for m in members)
        desc = max((m.description for m in members), key=len, default="")

        merged[(slug, etype)] = ResolvedEntity(
            id=slug,
            canonical_name=canonical,
            entity_type=etype,
            description=desc,
            domains=all_domains,
            source_docs=all_docs,
            aliases=all_aliases,
            confidence=best_conf,
        )
    return merged


_FUZZY_SKIP_TYPES = frozenset({"Concept", "Document"})
_FUZZY_TYPE_SIZE_CAP = 2000
_FUZZY_SIM_THRESHOLD = 0.85


def _group_keys_by_type(
    merged: dict[tuple[str, str], ResolvedEntity],
) -> dict[str, list[tuple[str, str]]]:
    """Index merged keys by their entity-type field for per-type fuzzy passes."""
    by_type: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for key in merged:
        by_type[key[1]].append(key)
    return by_type


def _pick_winner(
    a_key: tuple[str, str],
    b_key: tuple[str, str],
    merged: dict[tuple[str, str], ResolvedEntity],
) -> tuple[tuple[str, str], tuple[str, str]]:
    """Return ``(victim, winner)`` where the winner has more source_docs."""
    a = merged[a_key]
    b = merged[b_key]
    if len(b.source_docs) > len(a.source_docs):
        return (a_key, b_key)
    return (b_key, a_key)


def _build_merge_map_for_type(
    slugs: list[tuple[str, str]],
    merged: dict[tuple[str, str], ResolvedEntity],
) -> dict[tuple[str, str], tuple[str, str]]:
    """O(n^2) Levenshtein scan inside one entity type; returns victim→winner map."""
    type_map: dict[tuple[str, str], tuple[str, str]] = {}
    for i in range(len(slugs)):
        if slugs[i] in type_map:
            continue
        for j in range(i + 1, len(slugs)):
            if slugs[j] in type_map:
                continue
            if _similarity(slugs[i][0], slugs[j][0]) < _FUZZY_SIM_THRESHOLD:
                continue
            victim, winner = _pick_winner(slugs[i], slugs[j], merged)
            type_map[victim] = winner
    return type_map


def _resolve_winner(start: tuple[str, str], merge_map: dict[tuple[str, str], tuple[str, str]]) -> tuple[str, str]:
    """Follow victim→winner chains until we hit a non-mapped winner."""
    winner = start
    while winner in merge_map:
        winner = merge_map[winner]
    return winner


def _apply_merge(
    merged: dict[tuple[str, str], ResolvedEntity],
    victim: tuple[str, str],
    winner: tuple[str, str],
) -> None:
    """Fold the victim entity into the winner in-place; victim popped from ``merged``."""
    v = merged.pop(victim)
    w = merged[winner]
    w.source_docs = _merge_lists(w.source_docs, v.source_docs)
    w.domains = _merge_lists(w.domains, v.domains)
    w.aliases = _merge_lists(w.aliases, [v.canonical_name], v.aliases)
    w.confidence = max(w.confidence, v.confidence)
    if len(v.description) > len(w.description):
        w.description = v.description


def fuzzy_match_and_merge_same_type(
    merged: dict[tuple[str, str], ResolvedEntity],
) -> dict[tuple[str, str], ResolvedEntity]:
    """O(n^2) fuzzy dedup within each entity type using Levenshtein similarity."""
    by_type = _group_keys_by_type(merged)
    merge_map: dict[tuple[str, str], tuple[str, str]] = {}

    for etype, keys in by_type.items():
        if etype in _FUZZY_SKIP_TYPES:
            continue
        slugs = sorted(keys, key=lambda k: k[0])
        if len(slugs) > _FUZZY_TYPE_SIZE_CAP:
            continue
        merge_map.update(_build_merge_map_for_type(slugs, merged))

    # Snapshot via tuple — ``_apply_merge`` pops from ``merged`` (not
    # ``merge_map``) so a tuple copy here is enough to keep the iteration
    # stable. ``tuple()`` is the canonical idiom; ``list()`` was an
    # unnecessary mutable copy (S7504).
    for victim in tuple(merge_map):
        winner = _resolve_winner(merge_map[victim], merge_map)
        _apply_merge(merged, victim, winner)

    return merged


def build_final_list(
    merged: dict[tuple[str, str], ResolvedEntity],
) -> list[ResolvedEntity]:
    """Sort resolved entities by (type, id) and return the final list."""
    return sorted(merged.values(), key=lambda e: (e.entity_type, e.id))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def resolve_entities(raw: list[RawEntity]) -> list[ResolvedEntity]:
    """Resolve raw entities into deduplicated canonical entities.

    Steps:
    1. Group by (slug, entity_type) — exact dedup
    2. Merge aliases and source docs within each group
    3. Fuzzy-match groups within same type (Levenshtein >= 0.85)
    4. Produce final ResolvedEntity list

    Args:
        raw: List of raw extracted entities.

    Returns:
        List of resolved, deduplicated entities.
    """
    groups = group_by_slug_and_type(raw)
    merged = merge_within_groups(groups)
    merged = fuzzy_match_and_merge_same_type(merged)
    return build_final_list(merged)
