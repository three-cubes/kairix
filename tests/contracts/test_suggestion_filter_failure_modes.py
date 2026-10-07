"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`SuggestionFilter`.

One method (``apply``). Failure surface:

  * ``raises`` — internal parse / dictionary lookup failures surface
    verbatim; the filter chain must not silently drop a suggestion on
    a code-level error (the suggestion would slip through unfiltered).
  * ``returns_empty`` — when every suggestion is filtered out the
    return is ``[]``, not None.

F43 parity: each body runs over a real shipped filter
(:mod:`kairix.knowledge.entities.filters`) AND the canonical
:class:`tests.fakes.FakeSuggestionFilter`.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from kairix.knowledge.entities.filters import ChainedSuggestionFilter, RolePhraseFilter
from kairix.knowledge.entities.protocols import Suggestion, SuggestionFilter
from tests.fakes import FakeSuggestionFilter

pytestmark = pytest.mark.contract

_ROLE_PHRASE: Suggestion = {"text": "the regional team", "label": "ORG", "source": "ner", "confidence": 0.9}


def _real_chain_with_crashing_member() -> SuggestionFilter:
    # The production chain composing a member whose ``apply`` crashes.
    return ChainedSuggestionFilter(
        filters=[RolePhraseFilter(), FakeSuggestionFilter(raises=RuntimeError("F68-filter-raises"))],
    )


def _fake_crashing_filter() -> SuggestionFilter:
    return FakeSuggestionFilter(raises=RuntimeError("F68-filter-raises"))


@pytest.mark.parametrize(
    "factory",
    [_real_chain_with_crashing_member, _fake_crashing_filter],
    ids=["real", "fake"],
)
def test_apply_raises_propagates_typed_exception(factory: Callable[[], SuggestionFilter]) -> None:
    """A filter parse failure surfaces — caller must not interpret a
    silently-empty return as "all suggestions filtered" when the filter
    crashed mid-pass.

    Sabotage proof: in ``ChainedSuggestionFilter.apply`` wrap the
    ``filt.apply(current, context)`` call in ``try/except Exception:
    continue``. Re-run: the real leg's pytest.raises sees nothing.
    Restored.
    """
    flt = factory()
    with pytest.raises(RuntimeError, match="F68-filter-raises"):
        flt.apply([{"text": "Acme", "label": "ORG", "source": "ner", "confidence": 0.9}], "context")


@pytest.mark.parametrize(
    "factory",
    [RolePhraseFilter, lambda: FakeSuggestionFilter(drop_texts=("the regional team",))],
    ids=["real", "fake"],
)
def test_apply_returns_empty_when_all_suggestions_filtered(factory: Callable[[], SuggestionFilter]) -> None:
    """When every suggestion is filtered out the return is ``[]`` —
    callers iterate without a None check.

    Sabotage proof: in ``RolePhraseFilter.apply`` return
    ``list(suggestions)`` instead of the filtered comprehension. Re-run:
    the real leg's ``== []`` fails. Restored.
    """
    flt = factory()
    out = flt.apply([_ROLE_PHRASE], "context")
    assert out == [], f"all-filtered must yield []; got {out!r}"
