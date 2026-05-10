"""Source-string regression guard against re-introducing hardcoded ``reference-library`` policy.

The 2026-05-05 reflib-pollution incident produced a hardcoded
``_RESERVED_COLLECTIONS = {"reference-library"}`` carve-out in
``kairix/core/search/resolver.py`` and a ``if target == "reference-library":``
branch in ``kairix/core/search/config_loader.py``. Both were deleted in
v2026.5.4 in favour of an operator-yaml-driven ``in_default`` flag.

This test asserts the literal string ``reference-library`` does not
re-appear in either source file — it is acceptable in docstrings and
comments only when *explaining* the historical context, but should never
be referenced in policy code. If you need to assert the literal name in
new code, you are about to re-create the foot-gun.

Justified callers that still legitimately reference the literal:
  - ``kairix/core/embed/cli.py`` — embed harness auto-injects a
    ``CollectionConfig(name="reference-library", ...)``. This is
    structural (the harness *is* the source of the name) and lives
    outside the resolver/config-loader policy surface.
  - ``kairix/core/search/registry.py`` — ``RESERVED_AGENT_COLLECTION_NAMES``
    structurally defends against agent-collection name collisions with
    that auto-injected name. Single-element constant, name-collision
    only, no policy intent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kairix.core.search import config_loader as config_loader_module
from kairix.core.search import resolver as resolver_module


@pytest.mark.contract
def test_resolver_source_does_not_reference_reflib_literal() -> None:
    """``DefaultCollectionResolver`` must not reference the literal collection name."""
    source = Path(resolver_module.__file__).read_text(encoding="utf-8")
    assert "reference-library" not in source, (
        "kairix/core/search/resolver.py reintroduces the hardcoded "
        "'reference-library' policy that was deliberately removed in "
        "v2026.5.4. Use the in_default flag on the collection in yaml; "
        "do NOT add new reserved-collection logic to the resolver."
    )


@pytest.mark.contract
def test_config_loader_resolve_retrieval_does_not_reference_reflib_literal() -> None:
    """``resolve_retrieval_config`` must not branch on the literal collection name.

    The reference-library retrieval baseline lives in operator yaml as a
    per-collection ``retrieval:`` block, not in source. The example yaml
    ships the historical baseline values so new operators get them by
    default; operators who deviate are taking deliberate ownership.
    """
    source = Path(config_loader_module.__file__).read_text(encoding="utf-8")
    # The string may legitimately appear in the explanatory docstring of
    # ``resolve_retrieval_config``; what we forbid is a policy branch that
    # tests against the literal at runtime.
    forbidden_patterns = [
        '== "reference-library"',
        "== 'reference-library'",
        '!= "reference-library"',
        "!= 'reference-library'",
        '"reference-library":',
    ]
    offenders = [pat for pat in forbidden_patterns if pat in source]
    assert not offenders, (
        f"kairix/core/search/config_loader.py contains forbidden reflib policy patterns: "
        f"{offenders}. Use per-collection retrieval overrides via yaml, not source branches."
    )
