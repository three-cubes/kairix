"""httpx client pool for transport.

See docs/architecture/provider-plugin-architecture.md. The pool caches
the OpenAI-compatible client process-wide so every coalescer batch
dispatch reuses the same ``httpx.Client`` connection pool, paying one
TLS handshake per process instead of one per batch.

Exports:

* ``ClientPool`` — DI-clean class (builder injected) for tests
* ``get_client`` — production singleton accessor
* ``reset_client_cache`` — drop the cached client (tests / credential
  rotation)

The Wave 1 scaffold (this directory) is now load-bearing for the
TLS-handshake fix in ``kairix/_azure.py``.
"""

from kairix.transport.pool.client_pool import (
    ClientPool,
    get_client,
    reset_client_cache,
)

__all__ = ["ClientPool", "get_client", "reset_client_cache"]
