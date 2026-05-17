"""OpenAI-direct provider plugin (scaffold).

Wave 1 stub: entry-point factory wired into ``pyproject.toml`` but
raises ``NotImplementedError`` until Wave 2 (IM-5) lands the
implementation that proves the Provider contract against a
non-Azure endpoint.

See ``docs/architecture/provider-plugin-architecture.md`` § Migration plan.
"""

from __future__ import annotations

from kairix.providers._base import Provider


def make_provider() -> Provider:
    """Construct the OpenAI-direct ``Provider`` (Wave 2: IM-5).

    Currently raises ``NotImplementedError``; entry-point registration
    keeps discovery green so Wave 2 can drop the implementation in
    without re-wiring metadata.
    """
    raise NotImplementedError(
        "openai provider lands in Wave 2 (IM-5). "
        "fix: track docs/architecture/provider-plugin-architecture.md "
        "Migration plan; "
        "next: IM-5 is the contract-proving plugin once IM-4 lands."
    )
