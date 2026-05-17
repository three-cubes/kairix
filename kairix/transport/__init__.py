"""kairix.transport — universal endpoint concerns layer.

See docs/architecture/provider-plugin-architecture.md for the
three-layer split (core / transport / providers). This package holds
one implementation of pool, retry, coalesce, cache, timeout, auth,
and telemetry that every provider plugin reuses — so a TLS-handshake
fix or pool tuning lands once, not per-provider.

This is the Wave 1 scaffold. No symbols are exported from the
top-level package yet; submodules (``kairix.transport.cache``,
``kairix.transport.coalesce``, ...) carry their own re-export shims
until Wave 2 flips the canonical path.
"""
