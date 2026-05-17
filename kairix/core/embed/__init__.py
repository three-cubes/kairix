"""Public surface for the kairix embed package.

Re-exports the embed entry points used by callers outside the package
boundary so they don't have to reach into private modules:

  - ``embed_text``: the cached single-text embed wrapper (delegates to
    :mod:`kairix._azure`). Tests and integration code import it from
    here rather than from the private ``kairix._azure`` module — F5
    (no private-name imports in tests).
"""

from kairix._azure import embed_text

__all__ = ["embed_text"]
