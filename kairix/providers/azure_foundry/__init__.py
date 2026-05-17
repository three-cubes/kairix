"""Azure Foundry provider plugin (scaffold).

Wave 1 stub: the entry-point factory is wired into ``pyproject.toml``
but raises ``NotImplementedError`` until Wave 2 (IM-4) extracts the
``kairix/_azure.py`` Foundry path into this package.

See ``docs/architecture/provider-plugin-architecture.md`` § Migration plan.
"""

from __future__ import annotations

from kairix.providers._base import Provider


def make_provider() -> Provider:
    """Construct the Azure Foundry ``Provider`` (Wave 2: IM-4).

    Currently raises ``NotImplementedError`` — kept as the registered
    entry-point factory so the discovery contract is exercised by
    Wave 1 tests and so Wave 2 can land the implementation without
    touching ``pyproject.toml``.
    """
    raise NotImplementedError(
        "azure_foundry provider lands in Wave 2 (IM-4). "
        "fix: track docs/architecture/provider-plugin-architecture.md "
        "Migration plan for the wave-2 dispatch; "
        "next: ETA is the IM-4 cherry-pick following SK-1..SK-7 scaffold."
    )
