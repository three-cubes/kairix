"""Vault-driven entity-override loader for the entity-suggest pipeline.

Closes #166. The dogfood-reported gap: spaCy's small NER model silently
misses or mistypes specific entities (company acronyms, project
codenames, Australian-specific orgs). The existing filter chain
(``KnownEntityAllowlist`` + ``NerLabelFilter``) accepts the right
configuration at the boundary — what was missing was a way for the
operator to drop a markdown file into the document store and have the
suggester read it.

This module is that loader. It parses a ``_entity-overrides.md`` file
and returns the three containers the filter chain accepts:

* ``allowlist`` — list of ``Suggestion`` dicts for
  :class:`KnownEntityAllowlist` (promotes terms NER missed).
* ``person_overrides`` — set of texts to force-label as ``PERSON``.
* ``org_overrides`` — set of texts to force-label as ``ORG``.

File format (one entry per markdown list item)::

    # Entity Overrides

    - "YYY": ORG
    - "AAA": ORG
    - "Jane Doe": PERSON
    - "bbb": ORG, case_insensitive: true

Each line: ``- "<term>": <LABEL>`` followed by an optional comma-separated
flag list. The only flag currently honoured is ``case_insensitive: true``
which expands the entry into both the lower-cased and title-cased
variants of the term — handy for acronyms that appear in mixed case
across documents.

Recognised labels: ``ORG``, ``PERSON``, ``GPE``, ``PRODUCT``, ``WORK_OF_ART``.
Anything else logs a warning and is skipped.

Defensive by design: missing file → empty result with no warning.
Malformed file → warning + empty result. The override layer must
**never** block entity suggest.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from kairix.knowledge.entities.protocols import Suggestion

logger = logging.getLogger(__name__)

# Labels the filter chain knows how to act on. Anything outside this set
# is silently allowlisted but won't drive NerLabelFilter overrides.
_VALID_LABELS: frozenset[str] = frozenset({"ORG", "PERSON", "GPE", "PRODUCT", "WORK_OF_ART"})

# Head of a single entry: leading ``- `` then a quoted term, then
# ``: LABEL``. The quotes around the term are required so terms
# containing colons/commas don't trip the parser. The optional
# ``, key: value`` flag tail is matched separately by ``_FLAG_PATTERN``
# below — splitting the two patterns keeps each regex below SonarCloud's
# cognitive-complexity ceiling.
_ENTRY_HEAD_PATTERN = re.compile(
    r"""^\s*-\s+              # leading list marker
        "(?P<term>[^"]+)"     # quoted term
        \s*:\s*
        (?P<label>[A-Z_]+)    # uppercase label
        (?P<tail>.*)$         # everything after the label — flags parsed separately
    """,
    re.VERBOSE,
)
# Tail must be either empty/whitespace or a sequence of ``, key: value``
# flag entries. Used to validate the tail before feeding it to
# ``_FLAG_PATTERN``; a non-matching tail means the entry is malformed.
_ENTRY_TAIL_PATTERN = re.compile(
    r"^(?:\s*,\s*[A-Za-z_]+\s*:\s*[A-Za-z0-9]+)*\s*$",
)
_FLAG_PATTERN = re.compile(r"\s*,\s*(?P<key>[A-Za-z_]+)\s*:\s*(?P<value>[A-Za-z0-9]+)")


@dataclass(frozen=True)
class EntityOverrides:
    """Resolved overrides for the entity-suggest filter chain.

    ``allowlist`` plugs into :class:`KnownEntityAllowlist`;
    ``person_overrides`` and ``org_overrides`` plug into
    :class:`NerLabelFilter`. All three fields default to empty
    containers so callers can build a chain without ceremony when no
    override file is present.
    """

    allowlist: list[Suggestion] = field(default_factory=list)
    person_overrides: set[str] = field(default_factory=set)
    org_overrides: set[str] = field(default_factory=set)


def load_entity_overrides(path: Path | None) -> EntityOverrides:
    """Read and parse the override file at ``path``.

    ``path`` is the explicit path to the markdown file (or ``None`` to
    skip — useful when the caller has resolved the file location and
    wants to short-circuit a missing file without raising).

    Never raises. A missing file returns an empty :class:`EntityOverrides`
    silently; a malformed file logs a warning and returns whatever rows
    parsed successfully before the first failure.
    """
    if path is None:
        return EntityOverrides()
    try:
        if not path.exists():
            return EntityOverrides()
    except OSError as exc:
        logger.warning("entity-overrides: cannot stat %s — %s", path, exc)
        return EntityOverrides()

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("entity-overrides: cannot read %s — %s", path, exc)
        return EntityOverrides()

    return _parse(raw, source=str(path))


def _parse(raw: str, *, source: str) -> EntityOverrides:
    """Parse the markdown body into the override containers.

    Each list item that matches the entry grammar contributes one or
    more entries via :func:`_parse_entry`; anything else is ignored
    (so prose, headings, blank lines in the file are harmless).
    """
    allowlist: list[Suggestion] = []
    person_overrides: set[str] = set()
    org_overrides: set[str] = set()

    for lineno, line in enumerate(raw.splitlines(), start=1):
        parsed = _parse_entry(line, lineno=lineno, source=source)
        if parsed is None:
            continue
        label, variants = parsed
        for variant in variants:
            allowlist.append({"text": variant, "label": label, "source": "allowlist", "confidence": 1.0})
            if label == "PERSON":
                person_overrides.add(variant)
            elif label == "ORG":
                org_overrides.add(variant)

    return EntityOverrides(
        allowlist=allowlist,
        person_overrides=person_overrides,
        org_overrides=org_overrides,
    )


def _parse_entry(line: str, *, lineno: int, source: str) -> tuple[str, list[str]] | None:
    """Parse one line into ``(label, surface_forms)`` or ``None``.

    Returns ``None`` for blank lines, non-list lines, and unparseable
    entries (the last logs a warning so operators see typos). The list
    of surface forms is the expansion driven by the ``case_insensitive``
    flag — callers iterate it and route each variant by label.
    """
    stripped = line.strip()
    if not stripped or not stripped.startswith("-"):
        return None

    head = _ENTRY_HEAD_PATTERN.match(line)
    if head is None or not _ENTRY_TAIL_PATTERN.match(head.group("tail")):
        logger.warning(
            "entity-overrides: %s:%d skipping unparseable entry %r",
            source,
            lineno,
            stripped,
        )
        return None

    term = head.group("term").strip()
    label = head.group("label").strip()
    if not term or not label:
        logger.warning(
            "entity-overrides: %s:%d empty term or label in %r",
            source,
            lineno,
            stripped,
        )
        return None
    if label not in _VALID_LABELS:
        logger.warning(
            "entity-overrides: %s:%d unknown label %r (allowed: %s)",
            source,
            lineno,
            label,
            sorted(_VALID_LABELS),
        )
        return None

    flags = _parse_flags(head.group("tail") or "")
    variants = _expand_terms(term, case_insensitive=flags.get("case_insensitive", False))
    return label, variants


def _parse_flags(tail: str) -> dict[str, bool]:
    """Pull boolean flags out of the comma-separated tail.

    Only ``case_insensitive: true`` is recognised today; unknown flags
    log a warning and are dropped. Returned dict maps flag name to bool.
    """
    flags: dict[str, bool] = {}
    for fm in _FLAG_PATTERN.finditer(tail):
        key = fm.group("key").lower()
        value = fm.group("value").lower()
        if key == "case_insensitive":
            flags["case_insensitive"] = value in {"true", "1", "yes"}
        else:
            logger.warning("entity-overrides: ignoring unknown flag %r", key)
    return flags


def _expand_terms(term: str, *, case_insensitive: bool) -> list[str]:
    """Return the surface forms to register for ``term``.

    With ``case_insensitive=False`` (default) the term is registered as
    written. With ``case_insensitive=True`` the term is also registered
    as upper-case, lower-case, and title-case so any input casing
    matches — the filter chain uses string-equality on text, so the
    expansion has to happen at load time, not lookup time.
    """
    if not case_insensitive:
        return [term]
    variants = {term, term.upper(), term.lower(), term.title()}
    return sorted(variants)
