"""Provider plugin Protocol layer.

Defines the universal contract every LLM/embed endpoint family must
satisfy and the discovery mechanism (Python entry points) that lets
operators select a provider by configuration string rather than by
``import`` statement.

The split this module enforces:

- ``Provider`` Protocol — one LLM/embed endpoint family
  (Azure Foundry, OpenAI, Bedrock, Ollama, LiteLLM-proxy, Anthropic, ...).
  Every plugin implements it; core code only ever imports the Protocol,
  never a concrete provider.

- ``ProviderRegistry`` Protocol — resolves a configured name to a
  ``Provider`` instance. Production wires it via the entry-points
  mechanism (``EntryPointRegistry``); tests inject ``FakeProviderRegistry``
  from ``tests/fakes.py``.

- ``ProviderNotRegistered`` — typed exception with the unknown name
  plus the list of currently-installed provider names so operators get
  an actionable error when they typo ``KAIRIX_PROVIDER``.

- ``EntryPointRegistry`` — concrete ``ProviderRegistry`` implementation
  that calls ``importlib.metadata.entry_points(group="kairix.providers",
  name=name)`` for name-filtered lookup (sub-10 ms on Python 3.10+).

- ``get_provider(name, registry=None)`` — convenience accessor that
  defaults to a fresh ``EntryPointRegistry``; tests pass an explicit
  ``FakeProviderRegistry``.

See ``docs/architecture/provider-plugin-architecture.md`` for the
architectural decision record that defines this split.
"""

from __future__ import annotations

import importlib.metadata
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

#: Canonical entry-points group name. Third-party provider distributions
#: declare entries in this group via ``[project.entry-points."kairix.providers"]``
#: in their own ``pyproject.toml``; ``pip install kairix-provider-foo`` then
#: ``KAIRIX_PROVIDER=foo`` works with zero kairix code change.
ENTRY_POINT_GROUP = "kairix.providers"


@dataclass(frozen=True)
class ProviderHealth:
    """Synchronous healthcheck result returned by ``Provider.healthcheck``.

    Carries the bare minimum operators need from ``kairix probe-config``:
    is the endpoint reachable, what URL was probed, and how long the
    cold/warm round-trips took. ``error`` carries the short failure
    string when ``ok`` is False; otherwise it is ``None``.
    """

    ok: bool
    endpoint: str
    cold_ms: float | None = None
    warm_ms: float | None = None
    error: str | None = None


@runtime_checkable
class Provider(Protocol):
    """One LLM/embed endpoint family.

    Implementations live under ``kairix/providers/<name>/`` and register
    via the ``kairix.providers`` entry-point group in ``pyproject.toml``.
    Core code never imports a concrete provider — only this Protocol.

    Members:

    - ``name`` (``str``): short stable name ("azure_foundry" | "openai" |
      "bedrock" | ...). Matches the entry-point key under
      ``[project.entry-points."kairix.providers"]``.
    - ``embed_batch(texts)``: batch embed N texts in one HTTP call. Never
      raises; returns ``[]`` entries for failures so callers can
      short-circuit per-text rather than abort the batch.
    - ``chat(messages, *, max_tokens=800)``: single chat completion.
      Never raises; returns ``""`` on failure.
    - ``dimension()``: embedding vector dimension; constant per deployed
      model.
    - ``healthcheck()``: synchronous probe — does the configured endpoint
      respond? Used by ``kairix probe-config`` and the operator-facing
      CLI to surface a one-line green/red status plus cold/warm latency.
    """

    name: str

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Batch embed; never raises; returns ``[]`` per text on failure."""

    def chat(self, messages: list[dict[str, Any]], *, max_tokens: int = 800) -> str:
        """Single chat completion; never raises; returns ``""`` on failure."""

    def dimension(self) -> int:
        """Embedding vector dimension; constant per deployed model."""

    def healthcheck(self) -> ProviderHealth:
        """Synchronous endpoint probe; returns a ``ProviderHealth`` record."""


@runtime_checkable
class ProviderRegistry(Protocol):
    """Resolves a configured provider name to a ``Provider`` instance.

    Production wires this via ``EntryPointRegistry`` (entry-points
    discovery); tests inject ``FakeProviderRegistry`` from
    ``tests/fakes.py`` with a name→Provider mapping.

    Members:

    - ``resolve(name)``: return the ``Provider`` registered under
      ``name``; raises ``ProviderNotRegistered`` (with the populated
      ``available`` list) when ``name`` is unknown.
    - ``available()``: sorted list of currently-installed provider names;
      used to populate ``ProviderNotRegistered.available`` and by the
      operator-facing CLI to enumerate plugin choices.
    """

    def resolve(self, name: str) -> Provider:
        """Return the registered ``Provider`` or raise ``ProviderNotRegistered``."""

    def available(self) -> list[str]:
        """Sorted list of currently-installed provider names."""


class ProviderNotRegistered(LookupError):  # noqa: N818 — name is the public contract pinned by the provider-plugin ADR (docs/architecture/provider-plugin-architecture.md § Plugin discovery); renaming to *Error breaks third-party plugin distributions that catch the exact symbol.
    """Raised when an operator selects a provider name no plugin claims.

    Carries the requested ``name`` plus the sorted list of currently-
    installed provider names so the CLI can render an actionable error
    ("did you mean ...?") rather than a bare ``KeyError``.
    """

    def __init__(self, name: str, available: list[str]) -> None:
        self.name = name
        self.available = list(available)
        if available:
            installed = ", ".join(available)
            message = (
                f"No provider registered under name {name!r}. "
                f"Installed providers: {installed}. "
                f"fix: set KAIRIX_PROVIDER to one of the installed names, "
                f"or pip install the third-party distribution that ships {name!r}."
            )
        else:
            message = (
                f"No provider registered under name {name!r}. "
                f"No providers are currently installed. "
                f"fix: pip install -e . in the kairix checkout to register "
                f"the first-party plugins, or pip install a third-party "
                f"kairix-provider-* distribution."
            )
        super().__init__(message)


# Type alias for the entry-points callable EntryPointRegistry consumes.
# Matches the importlib.metadata.entry_points signature subset we use:
# called as ``entry_points(group=..., name=...)`` returning an iterable
# of objects with a ``.name`` attribute and a ``.load()`` method.
_EntryPointsCallable = Callable[..., Iterable[Any]]


class EntryPointRegistry:
    """Concrete ``ProviderRegistry`` backed by Python entry points.

    Discovers providers via
    ``importlib.metadata.entry_points(group="kairix.providers", name=name)``.
    First-party providers register in kairix's own ``pyproject.toml``;
    third parties ship a separate pip distribution that declares the
    same entry-point group.

    Resolution is name-filtered (not eager-scanned): sub-10 ms on
    Python 3.10+ and costs nothing for unused providers.

    The ``entry_points`` constructor parameter accepts the stdlib
    callable by default; tests pass a fake callable to exercise
    happy/failure paths without ``pip install -e .``.
    """

    def __init__(
        self,
        *,
        group: str = ENTRY_POINT_GROUP,
        entry_points: _EntryPointsCallable = importlib.metadata.entry_points,
    ) -> None:
        self._group = group
        self._entry_points = entry_points

    def resolve(self, name: str) -> Provider:
        eps = list(self._entry_points(group=self._group, name=name))
        if not eps:
            raise ProviderNotRegistered(name=name, available=self.available())
        factory = eps[0].load()
        provider: Provider = factory()
        return provider

    def available(self) -> list[str]:
        return sorted(ep.name for ep in self._entry_points(group=self._group))


def get_provider(name: str, registry: ProviderRegistry | None = None) -> Provider:
    """Resolve a provider name to a ``Provider`` instance.

    Defaults to a fresh ``EntryPointRegistry`` (production path); tests
    inject ``FakeProviderRegistry`` from ``tests/fakes.py``.
    """
    if registry is None:
        registry = EntryPointRegistry()
    return registry.resolve(name)
