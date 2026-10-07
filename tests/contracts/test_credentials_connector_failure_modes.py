"""F68 (ADR-024 Bundle A) — failure-mode contract for :class:`CredentialsConnector`.

Single Protocol method ``load_credentials(credentials)``. The Protocol
docstring pins the failure shape: returning ``None`` signals the
credential is invalid for this connector's source kind. Two failure
classes:

  * ``unauthorized`` — invalid credential shape returns ``None``
    (the "no" outcome — the connector REFUSES the credential and
    callers must distinguish from successful normalisation).
  * ``raises`` — credential normalisation crashes (KV unwrap fails,
    decrypt error, downstream token-fetch crashes). The Protocol
    surface must propagate, not swallow.

Every body runs over BOTH a shipped connector that validates its
credential — the real :class:`kairix.connectors.linear.LinearConnector`,
built through its ``client_builder`` seam with
:class:`tests.fakes.FakeLinearApiClient` — and the canonical
:class:`tests.fakes.FakeLinearConnector` (F43 behavioural parity).

Each test carries a "Sabotage proof:" comment describing the mutation
that proves the assertion has teeth.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from kairix.connectors.linear import LinearConnector, LinearCredentials
from kairix.core.protocols import CredentialsConnector
from tests.fakes import FakeLinearApiClient, FakeLinearConnector

pytestmark = pytest.mark.contract


def _real_connector() -> CredentialsConnector:
    return LinearConnector(
        credentials=LinearCredentials(api_key="lin_contract_fixture"),  # pragma: allowlist secret — test fixture
        client_builder=lambda _c: FakeLinearApiClient(pages={}),
    )


_IMPLEMENTATIONS: list[tuple[str, Callable[[], CredentialsConnector]]] = [
    ("real", _real_connector),
    ("fake", FakeLinearConnector),
]


class _KeyVaultBackedCredentials(dict[str, Any]):
    """A credential mapping whose values are unwrapped lazily from a
    secret store on access — every lookup fails as if the Key Vault
    unwrap surfaced a network error."""

    def get(self, key: str, default: Any = None) -> Any:
        raise RuntimeError(f"F68-kv-unwrap-failed for {key!r}")

    def __getitem__(self, key: str) -> Any:
        raise RuntimeError(f"F68-kv-unwrap-failed for {key!r}")


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_load_credentials_unauthorized_returns_none_for_invalid_blob(
    name: str, factory: Callable[[], CredentialsConnector]
) -> None:
    """A connector that rejects an invalid credential mapping MUST
    return ``None`` (NOT raise, NOT return a stale token) — the
    Protocol's documented "invalid" sentinel.

    Sabotage proof (executed): in ``LinearConnector.load_credentials``
    change the invalid-key ``return None`` to
    a dict carrying the raw (invalid) key. Re-run: the ``real`` case fails.
    Restored.
    """
    conn = factory()
    assert conn.load_credentials({"unrelated": "blob"}) is None, name
    assert conn.load_credentials({"api_key": "   "}) is None, name
    # Positive control: a usable key normalises identically on both impls.
    padded, normalised = " lin_key ", "lin_key"  # pragma: allowlist secret — test fixture value, not a credential
    assert conn.load_credentials({"api_key": padded}) == {"api_key": normalised}, name


@pytest.mark.parametrize("name,factory", _IMPLEMENTATIONS)
def test_load_credentials_raises_when_unwrap_fails(name: str, factory: Callable[[], CredentialsConnector]) -> None:
    """A connector whose credential transformation crashes (e.g. KV
    unwrap surfaces a network error) MUST raise — silent fallback to
    ``None`` would let callers fall through to an unauthenticated
    request.

    Sabotage proof: in ``LinearConnector.load_credentials`` wrap the
    ``credentials.get(...)`` lookups in ``try/except Exception: return None``.
    Re-run: the ``real`` case fails because no exception fires. Restored.
    """
    conn = factory()
    kv_ref = "secret/path"  # pragma: allowlist secret — test fixture value, not a credential
    with pytest.raises(RuntimeError, match="F68-kv-unwrap-failed"):
        conn.load_credentials(_KeyVaultBackedCredentials(api_key=kv_ref))
