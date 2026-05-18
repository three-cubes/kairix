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

This directory is load-bearing for the TLS-handshake fix the
provider plugins rely on — every plugin's transport client is built
via ``get_client`` so a single ``httpx.Client`` is reused across
batch dispatches.
"""

from kairix.transport.pool.client_pool import (
    ClientPool,
    get_client,
    reset_client_cache,
)

__all__ = ["ClientPool", "get_client", "reset_client_cache"]
