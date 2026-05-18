"""Re-export shim for ``kairix.transport.coalesce`` MUST yield the
*same class object* as the canonical path. ``is`` identity (not just
``==``) is the load-bearing assertion: a shim that re-implements or
re-defines the class would let callers split into two type identities
and break isinstance checks downstream.

The ``kairix.transport.cache`` package no longer has a shim — its
canonical implementation moved to :mod:`kairix.transport.cache` in
IM-3, so there is nothing for an identity test to compare against.
The package-level re-exports (``EmbedCache``, ``get_embed_cache``)
are exercised through every consumer's import path; see
``tests/transport/cache/test_embed_cache.py`` for the unit tests.

Sabotage-proof: each test was verified to fail when the shim's
import line was broken (e.g. importing a different symbol or a
typo'd module path). See commit body for details.

See docs/architecture/provider-plugin-architecture.md for the
three-layer split this shim package supports during the Wave 1
scaffold → Wave 2 move.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_embed_coalescer_class_is_canonical() -> None:
    """``kairix.transport.coalesce.EmbedCoalescer`` (package surface) is
    the SAME class object as
    ``kairix.transport.coalesce.embed_coalescer.EmbedCoalescer``
    (module surface).

    Wave 2 / IM-2 flipped the canonical home — the implementation now
    lives at ``kairix.transport.coalesce.embed_coalescer`` and the
    package ``__init__`` re-exports the names. ``is`` identity is the
    load-bearing assertion: a stray re-definition in the package
    ``__init__`` would split type identity and break isinstance checks.

    Sabotage-prove: change the package import to point at a stub
    ``class EmbedCoalescer: ...`` defined inside ``__init__`` →
    ``is`` comparison fails.
    """
    from kairix.transport.coalesce import EmbedCoalescer as PackageExport
    from kairix.transport.coalesce.embed_coalescer import EmbedCoalescer as Canonical

    assert PackageExport is Canonical


def test_get_embed_coalescer_is_canonical() -> None:
    """``kairix.transport.coalesce.get_embed_coalescer`` (package
    surface) is the SAME function object as
    ``kairix.transport.coalesce.embed_coalescer.get_embed_coalescer``.

    Sabotage-prove: drop ``get_embed_coalescer`` from the package
    ``__init__`` import line → ``ImportError`` at collection.
    """
    from kairix.transport.coalesce import get_embed_coalescer as package_export
    from kairix.transport.coalesce.embed_coalescer import get_embed_coalescer as canonical

    assert package_export is canonical


def test_reset_embed_coalescer_is_canonical() -> None:
    """``kairix.transport.coalesce.reset_embed_coalescer`` (package
    surface) is the SAME function object as
    ``kairix.transport.coalesce.embed_coalescer.reset_embed_coalescer``.
    Load-bearing because the autouse fixture in ``tests/conftest.py``
    calls the package-level name; a re-definition in the package
    ``__init__`` would let the singleton survive between tests through
    a divergent namespace.

    Sabotage-prove: drop ``reset_embed_coalescer`` from the package
    ``__init__`` import line → ``ImportError`` at collection.
    """
    from kairix.transport.coalesce import reset_embed_coalescer as package_export
    from kairix.transport.coalesce.embed_coalescer import (
        reset_embed_coalescer as canonical,
    )

    assert package_export is canonical
