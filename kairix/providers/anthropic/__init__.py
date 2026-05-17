"""Anthropic (x-api-key) provider plugin (scaffold).

Wave 1 stub: entry-point factory wired into ``pyproject.toml`` but
raises ``NotImplementedError`` until Wave 4 lands the implementation
(x-api-key header, Messages API format).

See ``docs/architecture/provider-plugin-architecture.md`` § Migration plan.
"""

from __future__ import annotations

from kairix.providers._base import Provider


def make_provider() -> Provider:
    """Construct the Anthropic ``Provider`` (Wave 4).

    Currently raises ``NotImplementedError``; entry-point registration
    keeps the discovery surface stable so Wave 4 can drop the
    implementation in additively.
    """
    raise NotImplementedError(
        "anthropic provider lands in Wave 4 (follow-up to issue #247). "
        "fix: track docs/architecture/provider-plugin-architecture.md "
        "Migration plan; "
        "next: ETA after Wave 3 measurement gate."
    )
