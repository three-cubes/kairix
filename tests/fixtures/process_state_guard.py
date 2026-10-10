"""Process-state guard: the runtime half of F1 + F2, a self-contained pytest plugin.

Registered from the root ``tests/conftest.py`` via ``pytest_plugins``. Kept
free of kairix / tests imports so ``tests/test_process_state_guard.py`` can
install this exact source as the conftest of a throwaway pytest run and prove
each rule end to end (the guarded names below are module constants the proof
overrides).

The static checks (``scripts/checks/check_no_env_monkeypatch.py`` /
``check_no_internal_patches.py``) give fast pre-commit feedback on the common
spellings. This plugin is the exact half: it watches what a test actually does
to shared process state, however it is spelled.

* **F2 — env writes.** One ``sys.addaudithook`` sees the ``os.putenv`` /
  ``os.unsetenv`` events CPython raises for every env write: ``os.environ[k] =``,
  ``update`` / ``pop`` / ``del`` / ``clear``, ``os.putenv``,
  ``patch.dict(os.environ, ...)``, ``monkeypatch.setenv`` / ``delenv``, and any
  ``os.__dict__`` route to the same mapping. A write of a guarded-prefix key
  while a test item runs (setup / call / teardown) or a test module is
  collected fails it. Replacing ``os.environ`` wholesale raises no such
  event, so the ``os.environ`` object is also identity-checked at the end of
  every phase and collection — a deleted or replaced attribute is put back
  before the failure is raised, so pytest's own reporting still finds it
  (and the patch wrappers reject ``os`` + ``"environ"`` targets up front).
* **F1 — sys.modules swaps.** The guarded ``sys.modules`` entries are
  snapshotted before each item's setup and compared at the end of its setup,
  call (fixture-applied patches still active) and teardown phases, and
  around each collection. A replaced or removed entry fails the item (or
  the module being collected). A NEW entry passes only if the import
  machinery made it: a module whose ``__spec__`` has a loader and whose
  ``__spec__.origin`` / ``__file__`` lies under the guarded package's
  directory. A bare ``ModuleType`` stub or any other object fails.
* **F1 — reloads.** The ``exec`` audit event fires whenever a module body
  runs. Every guarded module's file is recorded on its first execution (seeded
  at configure time from the guarded modules already imported); a second
  execution of the same file — during collection or a test — is a reload
  (``importlib.reload``, ``exec_module`` on the live module, or
  ``runpy.run_module`` of an imported module — drive a ``__main__`` guard
  with ``python -m`` in a subprocess instead).
* **F1 — patch APIs.** ``MonkeyPatch.setattr`` / ``delattr`` and
  ``mock.patch`` / ``patch.object`` (``_patch.__enter__``, which ``start()``
  and the decorator form also use) record a violation when the patched object
  is a guarded module or has a guarded ``__module__``, or a dotted string
  target names a guarded package. ``MonkeyPatch.setitem`` / ``delitem`` and
  ``mock.patch.dict`` on ``sys.modules`` with a guarded key (or
  ``clear=True``) are violations — caught when applied, so a swap undone
  inside the test body is still seen;
  ``monkeypatch.setitem(sys.modules, "yaml", None)`` (simulate a missing
  third-party dependency) stays allowed.

The audit hook only records; the item hooks turn records into ``pytest.fail``
(raising inside an audit hook would break the interpreter). The session env
baseline in ``tests/conftest.py`` wraps its own writes in
:func:`allow_baseline_writes` — the one sanctioned writer; called from any
other file it records a violation and exempts nothing.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

# Top-level packages whose modules tests must not swap, reload or patch (F1).
GUARDED_PACKAGES: tuple[str, ...] = ("kairix",)
# Process-env key prefixes tests must not write (F2).
GUARDED_ENV_PREFIXES: tuple[str, ...] = ("KAIRIX_",)
# The only file allowed to enter allow_baseline_writes(): the root conftest.
BASELINE_CONFTEST = Path(__file__).resolve().parent.parent / "conftest.py"

F2_MESSAGE = (
    "[F2] process-env write of a guarded key found in {where}: {details}. "
    "Refactor to pass the value through a seam (an env= mapping, "
    "paths=FakePaths(...), a *Deps field) to pass; a subprocess gets env= .\n"
    "Pass: resolve_dispatch_concurrency(env={{'KAIRIX_MAX_CONCURRENCY': '3'}})\n"
    "Forbidden: monkeypatch.setenv('KAIRIX_MAX_CONCURRENCY', '3')  "
    "# also os.environ[...] = / .pop / patch.dict(os.environ, ...) / os.putenv"
)
F1_MESSAGE = (
    "[F1] kairix-internal substitution found in {where}: {details}. "
    "Refactor to constructor injection with a fake from tests/fakes.py "
    "(or a *Deps / env= seam) to pass; reset module state via its public "
    "reset_*() function; to test a fresh import or a __main__ guard, run it in "
    "a subprocess (python -c / python -m).\n"
    "Pass: SearchPipeline(retriever=FakeRetriever(hits=[...])); "
    "monkeypatch.setitem(sys.modules, 'yaml', None)  # third-party dep missing\n"
    "Forbidden: monkeypatch.setattr(kairix.paths, 'x', fake); "
    "mock.patch('kairix.core.search.bm25.bm25_search'); "
    "sys.modules['kairix.x'] = stub; importlib.reload(kairix.x)"
)


class _State:
    """Mutable guard state shared by the audit hook and the pytest hooks."""

    def __init__(self) -> None:
        self.enabled = False  # between pytest_configure and pytest_unconfigure
        self.hook_installed = False  # audit hooks cannot be removed: install once
        self.active = False  # inside an item phase or a collection
        self.exempt = 0  # depth of allow_baseline_writes()
        self.env: list[str] = []
        self.patches: list[str] = []
        self.deferred_env: list[str] = []  # replacement details awaiting a successful patch apply
        self.modules: dict[str, Any] | None = None  # pre-setup snapshot
        self.environ: object | None = None  # the os.environ object at the snapshot
        self.module_files: set[str] = set()  # guarded module files executed so far
        self.package_dirs: tuple[str, ...] = ()  # guarded packages' directories


_STATE = _State()
_ORIGINALS: dict[tuple[type, str], Any] = {}


def _guarded_name(name: object) -> bool:
    return isinstance(name, str) and any(name == pkg or name.startswith(pkg + ".") for pkg in GUARDED_PACKAGES)


def _guarded_key(key: object) -> bool:
    if isinstance(key, bytes):
        key = key.decode("utf-8", "surrogateescape")
    return isinstance(key, str) and key.startswith(GUARDED_ENV_PREFIXES)


def _guarded_object(obj: object) -> bool:
    if isinstance(obj, ModuleType):
        return _guarded_name(obj.__name__)
    return _guarded_name(getattr(obj, "__module__", None))


def _recording() -> bool:
    return _STATE.enabled and _STATE.active and not _STATE.exempt


def _audit(event: str, args: tuple[Any, ...]) -> None:
    """Record guarded env writes and module re-executions. Never raises."""
    if event == "exec":
        code = args[0] if args else None
        if _STATE.enabled and getattr(code, "co_name", None) == "<module>":
            _record_module_exec(code.co_filename)
        return
    if event in ("os.putenv", "os.unsetenv") and _recording() and args and _guarded_key(args[0]):
        key = args[0].decode("utf-8", "surrogateescape") if isinstance(args[0], bytes) else args[0]
        _STATE.env.append(f"{'set' if event == 'os.putenv' else 'unset'} {key}")


def _under_package_dirs(path: object) -> bool:
    return isinstance(path, str) and path.startswith(_STATE.package_dirs)


def _record_module_exec(filename: str) -> None:
    """First execution of a guarded module's file records it; a second is a reload.

    Only files backing a guarded ``sys.modules`` entry count (the import
    machinery inserts the module, ``__file__`` set, before executing it), so
    package data compiled to code (templates) or a plugin file a host loads
    under its own non-guarded name never registers.
    """
    if filename in _STATE.module_files:
        if _recording():
            _STATE.patches.append(f"re-executed already-imported module file {filename}")
    elif _under_package_dirs(filename) and any(
        getattr(mod, "__file__", None) == filename for mod in _guarded_modules().values()
    ):
        _STATE.module_files.add(filename)


def allow_baseline_writes() -> contextlib.AbstractContextManager[None]:
    """Exempt the session env baseline's own writes (``tests/conftest.py``).

    Session-scoped fixtures set up inside the first item's setup and tear down
    inside the last item's teardown, so the baseline wraps its writes here.
    Only the root conftest may enter it: from any other file it records an F2
    violation and exempts nothing.
    """
    caller = Path(sys._getframe(1).f_code.co_filename).resolve()
    if caller != BASELINE_CONFTEST.resolve():
        if _recording():
            _STATE.env.append(f"allow_baseline_writes() entered from {caller} (only {BASELINE_CONFTEST} may)")
        return contextlib.nullcontext()
    return _exempt()


@contextlib.contextmanager
def _exempt() -> Iterator[None]:
    _STATE.exempt += 1
    try:
        yield
    finally:
        _STATE.exempt -= 1


def _guarded_modules() -> dict[str, Any]:
    return {name: mod for name, mod in list(sys.modules.items()) if _guarded_name(name)}


def _seed_package_state() -> None:
    """Locate the guarded packages and record the module files already executed."""
    dirs = []
    for pkg in GUARDED_PACKAGES:
        spec = importlib.util.find_spec(pkg)
        for location in (spec.submodule_search_locations or []) if spec else []:
            # raw + symlink-resolved, each with a trailing separator ("kairix/" never matches "kairix_x/")
            dirs += [str(Path(location)) + os.sep, str(Path(location).resolve()) + os.sep]
    _STATE.package_dirs = tuple(dirs)
    for mod in _guarded_modules().values():
        file = getattr(mod, "__file__", None)
        if isinstance(file, str):
            _STATE.module_files.add(file)


def _genuine_import(key: str, mod: object) -> bool:
    """A module the import machinery made from a file in a guarded package,
    registered under its own name (key == ``__name__`` == ``__spec__.name``)."""
    spec = getattr(mod, "__spec__", None)
    if not isinstance(mod, ModuleType) or spec is None or spec.loader is None:
        return False
    if not key == mod.__name__ == spec.name:
        return False
    locations = [spec.origin, getattr(mod, "__file__", None), *(spec.submodule_search_locations or [])]
    return any(_under_package_dirs(loc) for loc in locations)


def _module_swaps() -> list[str]:
    """Guarded sys.modules entries changed since the pre-setup snapshot."""
    before = _STATE.modules or {}
    now = _guarded_modules()
    found = [
        f"sys.modules[{name!r}] {'removed' if name not in now else 'replaced'}"
        for name, mod in before.items()
        if now.get(name, _MISSING) is not mod
    ]
    found += [
        f"sys.modules[{name!r}] inserted without the import machinery"
        for name, mod in now.items()
        if name not in before and not _genuine_import(name, mod)
    ]
    return found


_MISSING = object()


def _raise_recorded(where: str) -> None:
    """Fail with every violation recorded since the last check, then clear them."""
    env, patches = _STATE.env, _STATE.patches
    _STATE.env, _STATE.patches = [], []
    messages = []
    if env:
        messages.append(F2_MESSAGE.format(where=where, details="; ".join(dict.fromkeys(env))))
    if patches:
        messages.append(F1_MESSAGE.format(where=where, details="; ".join(dict.fromkeys(patches))))
    if messages:
        pytest.fail("\n\n".join(messages), pytrace=False)


# --- patch-API wrappers ------------------------------------------------------


def _wrap(owner: type, attr: str, check: Any, *, quiet_on_environ: bool = False) -> None:
    """Install ``check`` in front of ``owner.attr``.

    ``quiet_on_environ`` (``patch.dict``): once ``check`` has vetted the keys a
    ``patch.dict(os.environ, ...)`` sets, its apply and restore run without
    audit recording — the restore's ``clear()`` + ``update()`` would otherwise
    re-write every guarded key that was already there.
    """
    original = getattr(owner, attr)
    _ORIGINALS[(owner, attr)] = original

    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        mark = len(_STATE.deferred_env)
        detail = None
        try:
            if _recording() and check is not None:
                detail = check(self, *args, **kwargs)
            if quiet_on_environ and _is_environ_target(self.in_dict):
                with _exempt():
                    result = original(self, *args, **kwargs)
            else:
                result = original(self, *args, **kwargs)
            # Only an applied patch is a violation (F1 detail and F2 replacement
            # alike); a call that raised before changing anything is left to the
            # end-of-phase checks.
            if detail:
                _STATE.patches.append(detail)
            _STATE.env.extend(_STATE.deferred_env[mark:])
            return result
        finally:
            del _STATE.deferred_env[mark:]

    wrapper.__wrapped__ = original  # type: ignore[attr-defined]  # introspection aid only
    setattr(owner, attr, wrapper)


def _environ_replacement(owner: object, name: object) -> bool:
    """``os`` + ``"environ"`` (or the dotted ``"os.environ"``): replacing the mapping wholesale."""
    if isinstance(owner, str):  # never call == on an arbitrary patched object
        return owner == "os.environ"
    return owner is os and isinstance(name, str) and name == "environ"


def _check_monkeypatch_attr(_mp: Any, target: object, name: object = None, *_a: Any, **_k: Any) -> str | None:
    if _environ_replacement(target, name):
        _STATE.deferred_env.append("os.environ replaced wholesale")
    if isinstance(target, str):
        return f"monkeypatch of dotted target {target!r}" if _guarded_name(target) else None
    if _guarded_object(target):
        return f"monkeypatch of {getattr(target, '__name__', type(target).__name__)}.{name}"
    return None


def _check_monkeypatch_item(_mp: Any, dic: object, name: object, *_a: Any, **_k: Any) -> str | None:
    if dic is sys.modules and _guarded_name(name):
        return f"monkeypatch of sys.modules[{name!r}]"
    return None


def _check_mock_patch(patcher: Any) -> str | None:
    try:
        target = patcher.getter()
    except Exception:
        return None  # an unresolvable target: let mock raise its own error
    if _environ_replacement(target, patcher.attribute):
        _STATE.deferred_env.append("os.environ replaced wholesale")
    if _guarded_object(target):
        return f"mock.patch of {getattr(target, '__name__', type(target).__name__)}.{patcher.attribute}"
    return None


def _is_environ_target(in_dict: object) -> bool:
    # os.environ may be deleted mid-phase: fall back to the phase snapshot.
    current = getattr(os, "environ", _MISSING)
    return in_dict is current or in_dict is _STATE.environ or (isinstance(in_dict, str) and in_dict == "os.environ")


def _check_mock_patch_dict(patcher: Any) -> str | None:
    in_dict, values = patcher.in_dict, patcher.values
    if _is_environ_target(in_dict):
        env_keys = [k for k in dict(values) if _guarded_key(k)]
        if patcher.clear or env_keys:
            _STATE.env.append(f"patch.dict(os.environ) of {env_keys[0] if env_keys else 'every key (clear=True)'}")
        return None
    if in_dict != "sys.modules" and in_dict is not sys.modules:
        return None
    keys = [k for k in dict(values) if _guarded_name(k)]
    if patcher.clear or keys:
        return f"mock.patch.dict of sys.modules[{keys[0]!r}]" if keys else "mock.patch.dict(sys.modules, clear=True)"
    return None


def _install_wrappers() -> None:
    from unittest import mock

    monkeypatch_cls = pytest.MonkeyPatch
    _wrap(monkeypatch_cls, "setattr", _check_monkeypatch_attr)
    _wrap(monkeypatch_cls, "delattr", _check_monkeypatch_attr)
    _wrap(monkeypatch_cls, "setitem", _check_monkeypatch_item)
    _wrap(monkeypatch_cls, "delitem", _check_monkeypatch_item)
    # Private classes: the single entry points of patch / patch.object (also
    # start() and the decorator form) and of patch.dict.
    _wrap(mock._patch, "__enter__", _check_mock_patch)  # type: ignore[attr-defined]  # private class, see above
    _wrap(mock._patch_dict, "_patch_dict", _check_mock_patch_dict, quiet_on_environ=True)  # type: ignore[attr-defined]  # private class, see above
    _wrap(mock._patch_dict, "_unpatch_dict", None, quiet_on_environ=True)  # type: ignore[attr-defined]  # private class, see above


# --- pytest hooks ------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    _seed_package_state()
    if not _STATE.hook_installed:
        sys.addaudithook(_audit)  # audit hooks cannot be removed; _STATE.enabled gates it
        _STATE.hook_installed = True
    if not _ORIGINALS:
        _install_wrappers()
    _STATE.enabled = True


def pytest_unconfigure(config: pytest.Config) -> None:
    _STATE.enabled = False
    for (owner, attr), original in _ORIGINALS.items():
        setattr(owner, attr, original)
    _ORIGINALS.clear()


@contextlib.contextmanager
def _watching(where: str) -> Iterator[None]:
    """Record while the body runs, then check the sys.modules snapshot; fail
    ``where`` with anything recorded.

    When the body itself raised, a recorded violation still wins (chained to
    the original error); otherwise the original error propagates unchanged.
    """
    _STATE.active = True
    try:
        yield
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        _end_phase()
        _raise_recorded(where)
        raise
    finally:
        _STATE.active = False
    _end_phase()
    _raise_recorded(where)


def _end_phase() -> None:
    _STATE.active = False
    if _STATE.enabled and _STATE.modules is not None:
        _STATE.patches.extend(_module_swaps())
    if _STATE.enabled and _STATE.environ is not None:
        current = getattr(os, "environ", _MISSING)
        if current is not _STATE.environ:
            # Put the real mapping back first: pytest's own reporting reads
            # os.environ, so a deleted / replaced attribute would otherwise
            # turn this into an internal error instead of an [F2] failure.
            os.environ = _STATE.environ  # type: ignore[assignment]  # noqa: B003  # F2-RESTORE: snapshot back
            _STATE.env.append("os.environ deleted" if current is _MISSING else "os.environ replaced wholesale")


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_setup(item: pytest.Item):
    _STATE.env, _STATE.patches = [], []
    _STATE.modules, _STATE.environ = _guarded_modules(), os.environ
    with _watching(item.nodeid):
        return (yield)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_call(item: pytest.Item):
    with _watching(item.nodeid):
        return (yield)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None):
    try:
        with _watching(item.nodeid):
            return (yield)
    finally:
        _STATE.modules = _STATE.environ = None


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_make_collect_report(collector: pytest.Collector):
    """Collection runs under the same lifecycle as a test phase: snapshot the
    guarded sys.modules entries, record while collecting, compare after (and
    put back whatever snapshot / recording state was in force before)."""
    prior = _STATE.modules, _STATE.environ, _STATE.active
    _STATE.modules, _STATE.environ = _guarded_modules(), os.environ
    _STATE.active = True
    try:
        report = yield
    finally:
        _end_phase()
        _STATE.modules, _STATE.environ, _STATE.active = prior
    try:
        _raise_recorded(collector.nodeid or "collection")
    except pytest.fail.Exception as exc:
        report.outcome = "failed"
        report.longrepr = str(exc)
    return report
