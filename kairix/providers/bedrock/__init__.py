"""AWS Bedrock provider plugin (scaffold).

Wave 1 stub: entry-point factory wired into ``pyproject.toml`` but
raises ``NotImplementedError`` until Wave 4 lands the implementation
(SigV4 auth, model-id dispatch).

See ``docs/architecture/provider-plugin-architecture.md`` § Migration plan.
"""

from __future__ import annotations

from kairix.providers._base import Provider


def make_provider() -> Provider:
    """Construct the Bedrock ``Provider`` (Wave 4).

    Currently raises ``NotImplementedError``; entry-point registration
    keeps the discovery surface stable so Wave 4 can drop the
    implementation in additively (one commit per new provider).
    """
    raise NotImplementedError(
        "bedrock provider lands in Wave 4 (follow-up to issue #247). "
        "fix: track docs/architecture/provider-plugin-architecture.md "
        "Migration plan; "
        "next: depends on Wave 3 verification of the azure_foundry + "
        "openai contract."
    )
