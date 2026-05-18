"""Uniform timings-hook protocol for transport.

See docs/architecture/provider-plugin-architecture.md. The probe in
``kairix/quality/probe/`` reads from this hook to measure every layer
through one contract — no provider conditionals. Wave 2 lands the
Protocol definition (uniform stage keys ``pool_acquire``,
``coalesce_wait``, ``cache_lookup``, ``retry_attempts``, ...).
Placeholder for now so the package layout is in place.
"""
