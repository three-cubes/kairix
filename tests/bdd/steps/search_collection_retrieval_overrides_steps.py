"""Step definitions for search_collection_retrieval_overrides.feature.

Pins the **current** contract for ``resolve_retrieval_config``: the resolver
no longer hardcodes a REFLIB_RETRIEVAL_CONFIG identity for the
"reference-library" collection (#158). Instead, operators carry their own
per-collection overrides in YAML, and the resolver merges them over the
global config when the search is scoped to a single collection.

These scenarios drive ``resolve_retrieval_config`` via its public injection
seam (``deps=ResolveConfigDeps(config_fn, overrides_fn)``) — no
monkeypatching, no inline stubs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

from kairix.core.search.config import RetrievalConfig
from kairix.core.search.config_loader import ResolveConfigDeps, resolve_retrieval_config


@dataclass
class _OverridesCtx:
    global_strategy: str = "rrf"
    overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    resolved: RetrievalConfig | None = None


@pytest.fixture
def overrides_ctx() -> _OverridesCtx:
    return _OverridesCtx()


# ---------------------------------------------------------------------------
# Given
# ---------------------------------------------------------------------------


@given(parsers.parse('a kairix config with a global retrieval default of "{strategy}"'))
def _set_global_strategy(overrides_ctx: _OverridesCtx, strategy: str) -> None:
    overrides_ctx.global_strategy = strategy


@given(parsers.parse('the operator declares a per-collection override on "{collection}":'))
def _set_collection_override(
    overrides_ctx: _OverridesCtx,
    collection: str,
    datatable: list[list[str]],
) -> None:
    headers = datatable[0]
    rows = datatable[1:]
    payload: dict[str, Any] = {}
    for field_name, raw_value in (dict(zip(headers, row, strict=True)).values() for row in rows):
        # Cast the typed YAML values: ints stay ints, everything else stays str.
        try:
            payload[field_name] = int(raw_value)
        except ValueError:
            payload[field_name] = raw_value
    overrides_ctx.overrides[collection] = payload


# ---------------------------------------------------------------------------
# When
# ---------------------------------------------------------------------------


def _resolve_with_ctx(ctx: _OverridesCtx, *, collection: str | None, collections: list[str] | None) -> None:
    """Resolve via the public seams — no real YAML on disk needed."""
    global_cfg = RetrievalConfig(fusion_strategy=ctx.global_strategy)
    ctx.resolved = resolve_retrieval_config(
        collection=collection,
        collections=collections,
        deps=ResolveConfigDeps(
            config_fn=lambda: global_cfg,
            overrides_fn=lambda: ctx.overrides,
        ),
    )


@when(parsers.parse('the resolver is asked for the retrieval config for "{collection}"'))
def _resolve_single(overrides_ctx: _OverridesCtx, collection: str) -> None:
    _resolve_with_ctx(overrides_ctx, collection=collection, collections=None)


@when(parsers.parse('the resolver is asked for the retrieval config for collections "{csv}"'))
def _resolve_multi(overrides_ctx: _OverridesCtx, csv: str) -> None:
    cols = [c.strip() for c in csv.split(",") if c.strip()]
    _resolve_with_ctx(overrides_ctx, collection=None, collections=cols)


# ---------------------------------------------------------------------------
# Then
# ---------------------------------------------------------------------------


@then(parsers.parse('the resolved fusion_strategy is "{strategy}"'))
def _assert_fusion_strategy(overrides_ctx: _OverridesCtx, strategy: str) -> None:
    assert overrides_ctx.resolved is not None
    assert overrides_ctx.resolved.fusion_strategy == strategy, (
        f"expected fusion_strategy={strategy!r}, got {overrides_ctx.resolved.fusion_strategy!r}"
    )


@then(parsers.parse("the resolved vec_limit is {value:d}"))
def _assert_vec_limit(overrides_ctx: _OverridesCtx, value: int) -> None:
    assert overrides_ctx.resolved is not None
    assert overrides_ctx.resolved.vec_limit == value, (
        f"expected vec_limit={value}, got {overrides_ctx.resolved.vec_limit}"
    )
