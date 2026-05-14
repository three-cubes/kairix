"""kairix plugins — adapters into third-party agent runtimes (#246 W5).

Each subpackage hosts the plugin assets for one runtime. Today the only
runtime is openclaw — see :mod:`kairix.plugins.openclaw`.

Plugin code is kept inside the ``kairix`` Python package so it ships with
``pip install kairix`` and is discoverable at a canonical install path
(``<site-packages>/kairix/plugins/<runtime>/<plugin-dir>``). The Docker
image symlinks each plugin into ``/opt/kairix/plugins/<runtime>/`` so
admins point openclaw at a stable path rather than chasing Python's
site-packages location.
"""
