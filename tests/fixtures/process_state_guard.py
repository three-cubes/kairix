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
  collected fails it.
* **F1 — sys.modules swaps.** The guarded ``sys.modules`` entries are
  snapshotted before each item's setup and compared at the end of its call
  phase (fixture-applied patches still active). A replaced or removed entry
  fails the item; a newly imported module is fine.
* **F1 — reloads.** The ``exec`` audit event fires whenever a module body
  runs. A ``<module>`` code object whose file belongs to a module that was
  already imported before the item started is a re-execution
  (``importlib.reload``, ``exec_module`` on the live module, or
  ``runpy.run_module`` of an imported module — drive a ``__main__`` guard
  with ``python -m`` in a subprocess instead); a first import is not in the
  snapshot and passes.
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
:func:`allow_baseline_writes` — the one sanctioned writer.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Iterator
from types import ModuleType
from typing import Any

import pytest

# Top-level packages whose modules tests must not swap, reload or patch (F1).
GUARDED_PACKAGES: tuple[str, ...] = ("kairix",)
# Process-env key prefixes tests must not write (F2).
GUARDED_ENV_PREFIXES: tuple[str, ...] = ("KAIRIX_",)

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
        self.active = False  # inside an item phase or a collection
        self.exempt = 0  # depth of allow_baseline_writes()
        self.env: list[str] = []
        self.patches: list[str] = []
        self.modules: dict[str, ModuleType] | None = None  # pre-setup snapshot
        self.module_files: frozenset[str] = frozenset()


_STATE = _State()
_HOOK_INSTALLED = False
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
        if (
            _recording()
            and getattr(code, "co_name", None) == "<module>"
            and getattr(code, "co_filename", None) in _STATE.module_files
        ):
            _STATE.patches.append(f"re-executed already-imported module file {code.co_filename}")
        return
    if event in ("os.putenv", "os.unsetenv") and _recording() and args and _guarded_key(args[0]):
        key = args[0].decode("utf-8", "surrogateescape") if isinstance(args[0], bytes) else args[0]
        _STATE.env.append(f"{'set' if event == 'os.putenv' else 'unset'} {key}")


@contextlib.contextmanager
def allow_baseline_writes() -> Iterator[None]:
    """Exempt the session env baseline's own writes (``tests/conftest.py``).

    Session-scoped fixtures set up inside the first item's setup and tear down
    inside the last item's teardown, so the baseline wraps its writes here.
    """
    _STATE.exempt += 1
    try:
        yield
    finally:
        _STATE.exempt -= 1


def _snapshot_modules() -> None:
    modules = {name: mod for name, mod in list(sys.modules.items()) if _guarded_name(name)}
    _STATE.modules = modules
    _STATE.module_files = frozenset(f for m in modules.values() if isinstance(f := getattr(m, "__file__", None), str))


def _module_swaps() -> list[str]:
    before = _STATE.modules or {}
    found = []
    for name, mod in before.items():
        now = sys.modules.get(name)
        if now is not mod:
            found.append(
                f"sys.modules[{name!r}] {'removed' if now is None and name not in sys.modules else 'replaced'}"
            )
    return found


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


def _wrap(owner: type, attr: str, check: Any) -> None:
    original = getattr(owner, attr)
    _ORIGINALS[(owner, attr)] = original

    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        if _recording():
            detail = check(self, *args, **kwargs)
            if detail:
                _STATE.patches.append(detail)
        return original(self, *args, **kwargs)

    wrapper.__wrapped__ = original  # type: ignore[attr-defined]  # introspection aid only
    setattr(owner, attr, wrapper)


def _check_monkeypatch_attr(_mp: Any, target: object, name: object = None, *_a: Any, **_k: Any) -> str | None:
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
    if _guarded_object(target):
        return f"mock.patch of {getattr(target, '__name__', type(target).__name__)}.{patcher.attribute}"
    return None


def _check_mock_patch_dict(patcher: Any) -> str | None:
    in_dict, values = patcher.in_dict, patcher.values
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
    _wrap(mock._patch_dict, "_patch_dict", _check_mock_patch_dict)  # type: ignore[attr-defined]  # private class, see above


# --- pytest hooks ------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    global _HOOK_INSTALLED
    if not _HOOK_INSTALLED:
        sys.addaudithook(_audit)  # audit hooks cannot be removed; _STATE.enabled gates it
        _HOOK_INSTALLED = True
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
    """Record while the body runs; fail ``where`` with anything recorded.

    When the body itself raised, a recorded violation still wins (chained to
    the original error); otherwise the original error propagates unchanged.
    """
    _STATE.active = True
    try:
        yield
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        _STATE.active = False
        _raise_recorded(where)
        raise
    finally:
        _STATE.active = False
    _raise_recorded(where)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_setup(item: pytest.Item):
    _STATE.env, _STATE.patches = [], []
    _snapshot_modules()
    with _watching(item.nodeid):
        return (yield)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_call(item: pytest.Item):
    with _watching(item.nodeid):
        try:
            return (yield)
        finally:
            if _STATE.enabled:
                _STATE.patches.extend(_module_swaps())


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None):
    try:
        with _watching(item.nodeid):
            return (yield)
    finally:
        _STATE.modules, _STATE.module_files = None, frozenset()


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_make_collect_report(collector: pytest.Collector):
    _STATE.active = True
    try:
        report = yield
    finally:
        _STATE.active = False
    try:
        _raise_recorded(collector.nodeid or "collection")
    except pytest.fail.Exception as exc:
        report.outcome = "failed"
        report.longrepr = str(exc)
    return report
