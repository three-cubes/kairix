"""openclaw plugin assets shipped with kairix (#246 W5).

Each subdirectory here is a self-contained openclaw plugin: a
``plugin.json`` manifest plus the Python entry module the manifest
points at. The directory name matches the on-disk slug openclaw scans
for in ``plugins.load.paths``; the ``"name"`` field in ``plugin.json``
is the identifier admins put in ``plugins.allow``.

Today's plugins:

- ``memory-prompt/`` — calls ``kairix bootstrap <agent>`` at session
  start and appends the resulting orientation envelope to the agent's
  system prompt (``appendSystemContext``). Failure mode is a short
  fallback string; the plugin never blocks session start.

The :func:`build_plugin` helper exposes the memory-prompt entry function
so unit tests can import it without depending on the openclaw runtime.
"""

from __future__ import annotations

from pathlib import Path


def memory_prompt_dir() -> Path:
    """Absolute path to the ``memory-prompt`` plugin directory.

    Useful for tests and for the docker image to symlink the plugin into
    ``/opt/kairix/plugins/openclaw/``. The directory name matches the
    on-disk slug openclaw discovers via ``plugins.load.paths``.
    """
    return Path(__file__).resolve().parent / "memory-prompt"


__all__ = ["memory_prompt_dir"]
