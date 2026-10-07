"""F68 failure-injection contract test for :class:`TokenStore`.

* ``store`` → ``unauthorized`` (KV / file backend rejects write)

F43 parity: ONE body runs over the real
:class:`~kairix.connect.store.azure_kv_store.AzureKeyVaultTokenStore`
(its ``client_factory=`` seam fed a :class:`FakeKeyVaultSecretClient`
that rejects the write) AND the canonical :class:`FakeTokenStore`.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from kairix.connect.protocols import (
    CapturedTokens,
    ClientCredentials,
    TokenStore,
    TokenStoreUnauthorizedError,
)
from kairix.connect.store.azure_kv_store import AzureKeyVaultTokenStore
from tests.fakes import FakeKeyVaultSecretClient, FakeTokenStore

pytestmark = pytest.mark.contract

_REJECTION = "simulated KV permission denied"


def _real_rejecting_store() -> TokenStore:
    secret_client = FakeKeyVaultSecretClient(raises=PermissionError(_REJECTION))
    return AzureKeyVaultTokenStore(
        vault_name="vault-alpha",
        env={},
        credential_factory=object,
        client_factory=secret_client.factory,
    )


def _fake_rejecting_store() -> TokenStore:
    return FakeTokenStore(raises=TokenStoreUnauthorizedError(_REJECTION))


@pytest.mark.parametrize(
    "factory",
    [_real_rejecting_store, _fake_rejecting_store],
    ids=["real", "fake"],
)
def test_store_unauthorized_when_backend_rejects_write(factory: Callable[[], TokenStore]) -> None:
    """A ``store`` call against a backend that refuses → :class:`TokenStoreUnauthorizedError`.

    Sabotage proof: in ``AzureKeyVaultTokenStore.store`` replace the
    ``raise TokenStoreUnauthorizedError(...) from exc`` with ``continue``;
    the real leg fails (no exception). Restored.
    """
    store = factory()
    with pytest.raises(TokenStoreUnauthorizedError, match="simulated KV"):
        store.store(
            scope="connector",
            area="gmail",
            instance=None,
            tokens=CapturedTokens(refresh_token="r", access_token="a", token_uri="https://x/"),
            client=ClientCredentials(client_id="c", client_secret="s"),
        )
