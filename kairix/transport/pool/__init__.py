"""httpx client pool for transport.

See docs/architecture/provider-plugin-architecture.md. ``client_pool``
and ``make_openai_client`` land here in Wave 2 (IM-1) along with the
TLS-handshake fix for ``kairix/_azure.py:_get_client`` (fresh
``httpx.Client`` per coalescer batch). Placeholder for now.
"""
