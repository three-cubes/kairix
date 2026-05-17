"""Credential resolution + secret lookup for transport.

See docs/architecture/provider-plugin-architecture.md. The real auth
move happens in Wave 2 (IM-1): ``get_credentials`` and ``get_secret``
relocate from ``kairix.secrets`` / ``kairix.credentials`` boundary
modules into this package. Until then this is a placeholder so the
package layout is in place.
"""
