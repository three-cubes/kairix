"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`SecretsResolver`.

``SecretsResolver.get`` returns ``None`` when no source resolves an
identity; ``require`` raises :class:`SecretNotFoundError` naming the
canonical secret. :func:`kairix.credentials.get_credentials` is the
consumer: a missing embed-tier secret must fall back to the LLM tier, a
missing Neo4j password must yield "no graph" (``None``), and a missing
required LLM secret must surface the typed error — never an empty-string
credential that fails later at the HTTP layer.

One body per method, two implementations (F43 limb 2):

* the real :class:`SecretsLoader` with an explicit env mapping and a KV
  mount directory that does not exist (nothing provisioned);
* the canonical :class:`tests.fakes.FakeSecretsLoader`.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from kairix.credentials import Credentials, get_credentials
from kairix.secrets import SecretNotFoundError, SecretsLoader, canonical_env_var
from tests.fakes import FakeSecretsLoader

pytestmark = pytest.mark.contract

_Identity = tuple[Any, str, str | None, str]
_LLM_KEY: _Identity = ("provider", "llm", None, "api-key")
_LLM_ENDPOINT: _Identity = ("provider", "llm", None, "endpoint")


def _real_loader(tmp_path: Path, values: dict[_Identity, str]) -> Any:
    env = {canonical_env_var(*identity): value for identity, value in values.items()}
    return SecretsLoader(env=env, kv_mount=tmp_path / "kv-mount-not-provisioned")


def _fake_loader(_tmp_path: Path, values: dict[_Identity, str]) -> Any:
    return FakeSecretsLoader(values=dict(values))


_FACTORIES: list[Callable[[Path, dict[_Identity, str]], Any]] = [_real_loader, _fake_loader]
_IDS = ["real-secrets-loader", "fake"]


@pytest.mark.parametrize("factory", _FACTORIES, ids=_IDS)
def test_get_returns_empty_for_unprovisioned_identity_credentials_fall_back(
    factory: Callable[[Path, dict[_Identity, str]], Any],
    tmp_path: Path,
) -> None:
    """``get`` misses (``None``) drive the consumer's documented fallbacks:
    the embed tier inherits the LLM credentials and the graph tier is off.

    Sabotage proof: in ``SecretsLoader.get`` change the final
    ``return self._try_kv_mount(canonical_kv)`` to
    ``return self._try_kv_mount(canonical_kv) or "unset"`` (a placeholder
    instead of a miss). Re-run: the real case fails — ``get`` is not
    ``None``, the embed tier keeps the placeholder key instead of falling
    back, and the graph tier comes back configured. Restored.
    """
    resolver = factory(tmp_path, {_LLM_KEY: "sk-llm-tier", _LLM_ENDPOINT: "https://llm.example.invalid"})

    assert resolver.get("provider", "embed", None, "api-key") is None
    embed = get_credentials("embed", secrets=resolver)
    assert isinstance(embed, Credentials)
    assert (embed.api_key, embed.endpoint) == ("sk-llm-tier", "https://llm.example.invalid")
    assert get_credentials("graph", secrets=resolver) is None


@pytest.mark.parametrize("factory", _FACTORIES, ids=_IDS)
def test_require_raises_secret_not_found_naming_canonical_secret(
    factory: Callable[[Path, dict[_Identity, str]], Any],
    tmp_path: Path,
) -> None:
    """With nothing provisioned, resolving LLM credentials raises the typed
    ``SecretNotFoundError`` naming the canonical KV secret.

    Sabotage proof: in ``SecretsLoader.require`` replace the
    ``raise SecretNotFoundError(...)`` with ``return ""``. Re-run: the real
    case fails because ``get_credentials("llm")`` returns a ``Credentials``
    with an empty api key instead of raising. Restored.
    """
    resolver = factory(tmp_path, {})

    with pytest.raises(SecretNotFoundError, match="kairix-provider-llm-api-key"):
        get_credentials("llm", secrets=resolver)
