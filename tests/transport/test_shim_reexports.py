"""Re-export shims for ``kairix.transport.cache`` and
``kairix.transport.coalesce`` MUST yield the *same class object* as
the canonical path. ``is`` identity (not just ``==``) is the
load-bearing assertion: a shim that re-implements or re-defines the
class would let callers split into two type identities and break
isinstance checks downstream.

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


def test_embed_cache_class_is_canonical() -> None:
    """``kairix.transport.cache.EmbedCache`` is the SAME class object
    as ``kairix.core.embed.embed_cache.EmbedCache``.

    Sabotage-prove: replace the shim's import with
    ``from kairix.transport.coalesce import EmbedCoalescer as EmbedCache``
    → ``is`` comparison fails (different class object).
    """
    from kairix.core.embed.embed_cache import EmbedCache as Canonical
    from kairix.transport.cache import EmbedCache as Shim

    assert Shim is Canonical


def test_get_embed_cache_is_canonical() -> None:
    """``kairix.transport.cache.get_embed_cache`` is the SAME function
    object as ``kairix.core.embed.embed_cache.get_embed_cache``.

    Sabotage-prove: remove ``get_embed_cache`` from the shim's import
    line → ``ImportError`` at collection.
    """
    from kairix.core.embed.embed_cache import get_embed_cache as canonical
    from kairix.transport.cache import get_embed_cache as shim

    assert shim is canonical


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
