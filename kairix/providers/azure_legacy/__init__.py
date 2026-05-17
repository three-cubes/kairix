"""Azure legacy (AzureOpenAI SDK) provider plugin (scaffold).

Wave 1 stub: entry-point factory wired into ``pyproject.toml`` but
raises ``NotImplementedError`` until Wave 2 lands the implementation.

See ``docs/architecture/provider-plugin-architecture.md`` § Migration plan.
"""

from __future__ import annotations

from kairix.providers._base import Provider


def make_provider() -> Provider:
    """Construct the Azure-legacy ``Provider`` (Wave 2).

    Currently raises ``NotImplementedError``; entry-point registration
    keeps discovery green so Wave 2 can drop the implementation in
    without re-wiring metadata.
    """
    raise NotImplementedError(
        "azure_legacy provider lands in Wave 2. "
        "fix: track docs/architecture/provider-plugin-architecture.md "
        "Migration plan; "
        "next: implementation follows IM-4 (azure_foundry) once the "
        "contract is proven."
    )
