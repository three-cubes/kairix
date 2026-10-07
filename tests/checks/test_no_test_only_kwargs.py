"""F6 broadened-suffix detector tests — covers the full
``_TEST_SEAM_SUFFIXES`` set, not just the original ``_fn`` shape.

The F6 detector (``scripts/checks/check_no_test_only_kwargs.py``) flags
``*_fn=None`` / ``*_loader=None`` / ``*_factory=None`` / ``*_builder=None`` /
``*_provider=None`` / ``*_resolver=None`` test-only kwargs on production
free functions. This test file pins each suffix, the absence of any
allow-list (PLA-472 — public and private hosts are always flagged), the ``ClassDef``-method exemption (constructor /
method injection IS the canonical Deps shape), and the dataclass-field
``default_factory=...`` safe path.

Three of the broadened-suffix tests carry executed sabotage proofs in
their docstring: a mutate→fail→restore loop the agent ran before
committing, documenting the line of the detector that — when mutated —
flips the assertion from green to red. Other broadened-suffix tests
follow the same shape but did not need an explicit sabotage docstring
because they share the same code path that the three executed sabotage
proofs exercise (`_has_test_seam_suffix` + the per-suffix iteration).

Pattern: write a small Python source string to a tmp file, parse it
through the public ``file_violations`` surface, and assert on the
returned list of qualified-param violation strings.
"""

from __future__ import annotations

# Add scripts/checks to sys.path so the detector module is importable.
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CHECKS_DIR = _REPO_ROOT / "scripts" / "checks"
if str(_CHECKS_DIR) not in sys.path:
    sys.path.insert(0, str(_CHECKS_DIR))

from check_no_test_only_kwargs import (  # noqa: E402 — see _CHECKS_DIR sys.path insert above; pinned by tests/conftest.py REPO_ROOT pattern
    _TEST_SEAM_SUFFIXES,
    file_has_violation,
    file_violations,
)

pytestmark = pytest.mark.unit


def _write_module(tmp_path: Path, source: str, name: str = "kairix_sample.py") -> Path:
    """Write ``source`` under a kairix-shaped path so ``_module_path``
    resolves to a kairix-prefixed module path.

    The detector treats every input file through the same code path
    regardless of where it lives — but ``main()`` only walks
    ``kairix/**``. Per-test we drop the file into a synthetic
    ``kairix/<tmp>/sample.py``-style location so the qualified violation
    string reads naturally and matches what ``main()`` would emit.
    """
    kairix_dir = tmp_path / "kairix" / "sample_pkg"
    kairix_dir.mkdir(parents=True, exist_ok=True)
    f = kairix_dir / name
    f.write_text(source, encoding="utf-8")
    return f


# ---------------------------------------------------------------------------
# Suffix coverage — each broadened suffix fires on a private free function
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_underscore_loader_kwarg_on_private_function_is_flagged(tmp_path: Path) -> None:
    """``def _resolve_thing(*, thing_loader=None)`` is a violation.

    Sabotage-proof (executed): commented out the ``"_loader"`` entry in
    ``_TEST_SEAM_SUFFIXES`` in
    ``scripts/checks/check_no_test_only_kwargs.py`` line 67; this test
    failed because ``_has_test_seam_suffix("thing_loader")`` returned
    False, ``file_violations`` returned ``[]`` and the assertion below
    flipped to True != False; restored the entry; test green again.
    """
    src = """
def _resolve_thing(*, thing_loader=None):
    return thing_loader() if thing_loader else "default"
"""
    f = _write_module(tmp_path, src, name="loader_priv.py")
    violations = file_violations(f)
    assert any(v.endswith("::_resolve_thing::thing_loader") for v in violations)


@pytest.mark.unit
def test_underscore_factory_kwarg_on_private_function_is_flagged(tmp_path: Path) -> None:
    """``def _resolve_thing(*, thing_factory=None)`` is a violation.

    Sabotage-proof (executed): mutated ``_TEST_SEAM_SUFFIXES`` to drop
    ``"_factory"``; this test failed (empty violations list); restored
    the entry; test green again. Documents the per-suffix loop the
    detector relies on.
    """
    src = """
def _resolve_thing(*, thing_factory=None):
    return thing_factory() if thing_factory else "default"
"""
    f = _write_module(tmp_path, src, name="factory_priv.py")
    violations = file_violations(f)
    assert any(v.endswith("::_resolve_thing::thing_factory") for v in violations)


@pytest.mark.unit
def test_underscore_builder_kwarg_on_private_function_is_flagged(tmp_path: Path) -> None:
    """``def _resolve_thing(*, thing_builder=None)`` is a violation.

    Sabotage-proof (executed): mutated ``_is_none_constant`` to
    unconditionally return False; this test failed because the
    ``_is_none_constant(default)`` guard short-circuits and no params
    are inspected; restored; test green again. Documents the
    none-default filter.
    """
    src = """
def _resolve_thing(*, thing_builder=None):
    return thing_builder() if thing_builder else "default"
"""
    f = _write_module(tmp_path, src, name="builder_priv.py")
    violations = file_violations(f)
    assert any(v.endswith("::_resolve_thing::thing_builder") for v in violations)


@pytest.mark.unit
def test_underscore_provider_kwarg_on_private_function_is_flagged(tmp_path: Path) -> None:
    """``def _resolve_thing(*, thing_provider=None)`` is a violation."""
    src = """
def _resolve_thing(*, thing_provider=None):
    return thing_provider() if thing_provider else "default"
"""
    f = _write_module(tmp_path, src, name="provider_priv.py")
    violations = file_violations(f)
    assert any(v.endswith("::_resolve_thing::thing_provider") for v in violations)


@pytest.mark.unit
def test_underscore_resolver_kwarg_on_private_function_is_flagged(tmp_path: Path) -> None:
    """``def _resolve_thing(*, thing_resolver=None)`` is a violation."""
    src = """
def _resolve_thing(*, thing_resolver=None):
    return thing_resolver() if thing_resolver else "default"
"""
    f = _write_module(tmp_path, src, name="resolver_priv.py")
    violations = file_violations(f)
    assert any(v.endswith("::_resolve_thing::thing_resolver") for v in violations)


# ---------------------------------------------------------------------------
# Regression: the original ``_fn`` shape still fires
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_underscore_fn_kwarg_on_private_function_is_flagged(tmp_path: Path) -> None:
    """The original ``_fn=None`` shape continues to fire after the broadening.

    Regression check — ensures the suffix-list change preserved the
    original behaviour.
    """
    src = """
def _resolve_thing(*, search_fn=None):
    return search_fn() if search_fn else "default"
"""
    f = _write_module(tmp_path, src, name="fn_priv.py")
    violations = file_violations(f)
    assert any(v.endswith("::_resolve_thing::search_fn") for v in violations)


# ---------------------------------------------------------------------------
# Suffix coverage on PUBLIC functions — same set fires; no allow-list
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_underscore_loader_kwarg_on_public_function_is_flagged(tmp_path: Path) -> None:
    """A public function ``def resolve_thing(*, thing_loader=None)`` is
    flagged — matches the ``_fn`` behaviour.
    """
    src = """
def resolve_thing(*, thing_loader=None):
    return thing_loader() if thing_loader else "default"
"""
    f = _write_module(tmp_path, src, name="loader_pub.py")
    violations = file_violations(f)
    assert any(v.endswith("::resolve_thing::thing_loader") for v in violations)


@pytest.mark.unit
def test_no_allow_list_can_rescue_a_flagged_param(tmp_path: Path) -> None:
    """PLA-472: F6 has no allow-list. The detector's public surface takes
    only the file path — there is no channel through which an allow set (or
    a re-created ``test-only-kwargs-allow-files.txt``) could rescue a
    flagged parameter, and the module no longer reads any allow file.

    Even with a file at the old allow-list location naming the exact
    qualified param, the param is still flagged.

    Sabotage proof (executed): re-adding an ``allow`` parameter to
    ``file_violations`` flips the signature assertion red; re-adding a
    module-level ``_read_allow_list`` flips the ``hasattr`` assertion red.
    Restored → green.
    """
    import inspect

    import check_no_test_only_kwargs as detector

    src = """
def _resolve_thing(*, thing_loader=None):
    return thing_loader() if thing_loader else "default"
"""
    f = _write_module(tmp_path, src, name="loader_priv_no_rescue.py")
    v = file_violations(f)
    assert len(v) == 1
    assert v[0].endswith("::_resolve_thing::thing_loader")
    allow_file = tmp_path / ".architecture" / "baseline" / "test-only-kwargs-allow-files.txt"
    allow_file.parent.mkdir(parents=True)
    allow_file.write_text(v[0] + "\n", encoding="utf-8")

    assert file_violations(f) == v
    assert list(inspect.signature(file_violations).parameters) == ["path"]
    assert not hasattr(detector, "_read_allow_list")
    assert not hasattr(detector, "_ALLOW_FILE")


# ---------------------------------------------------------------------------
# ClassDef-method exemption — constructor injection IS the canonical Deps
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_method_on_class_with_loader_kwarg_is_not_flagged(tmp_path: Path) -> None:
    """A method on a class with a ``thing_loader=None`` kwarg is NOT a
    violation — constructor / method injection on a class IS the
    canonical Deps shape, and F6 is about test-only kwargs on FREE
    functions, not on the Deps pattern.

    Sabotage-proof (executed): inverted the ``if _has_class_ancestor:
    continue`` branch in ``_iter_free_functions`` to ``if not
    _has_class_ancestor: continue`` (i.e. treat methods AS free
    functions); this test failed because the method was then flagged
    as a free-function violation; restored; test green again. Documents
    the ClassDef-ancestor exemption.
    """
    src = """
class Thing:
    def __init__(self, *, thing_loader=None):
        self.loader = thing_loader
"""
    f = _write_module(tmp_path, src, name="method.py")
    violations = file_violations(f)
    # The constructor's thing_loader kwarg must NOT appear in violations.
    assert not any("thing_loader" in v for v in violations)


@pytest.mark.unit
def test_method_on_nested_class_with_loader_kwarg_is_not_flagged(tmp_path: Path) -> None:
    """A method on a NESTED class is still exempt — the ancestor walk
    catches arbitrary nesting depth.
    """
    src = """
class Outer:
    class Inner:
        def do(self, *, thing_loader=None):
            return thing_loader
"""
    f = _write_module(tmp_path, src, name="nested_method.py")
    violations = file_violations(f)
    assert not any("thing_loader" in v for v in violations)


# ---------------------------------------------------------------------------
# Dataclass-field ``default_factory=...`` is safe — only ``= None`` flags
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dataclass_field_with_default_factory_is_not_flagged(tmp_path: Path) -> None:
    """A canonical Deps dataclass field is safe:

      ``write_state_fn: Callable[...] = field(default_factory=lambda: ...)``

    The value node is a ``Call``, not a ``None`` constant, so
    ``_is_none_constant`` returns False and the field is not flagged.
    """
    src = """
from dataclasses import dataclass, field
from typing import Callable

@dataclass
class Deps:
    write_state_fn: Callable[[], None] = field(default_factory=lambda: lambda: None)
"""
    f = _write_module(tmp_path, src, name="deps_dataclass.py")
    violations = file_violations(f)
    assert violations == []


@pytest.mark.unit
def test_dataclass_field_with_none_default_is_flagged(tmp_path: Path) -> None:
    """A dataclass field that defaults to ``None`` IS the F6 smell — same
    as a free-function kwarg shape, only relocated to a class field.
    """
    src = """
from dataclasses import dataclass
from typing import Callable

@dataclass
class Deps:
    write_state_fn: Callable[..., None] | None = None
"""
    f = _write_module(tmp_path, src, name="deps_none_field.py")
    violations = file_violations(f)
    assert any("write_state_fn" in v for v in violations)


@pytest.mark.unit
def test_dataclass_field_with_loader_none_default_is_flagged(tmp_path: Path) -> None:
    """The broadening also covers ``*_loader: ... = None`` on a class field."""
    src = """
from dataclasses import dataclass
from typing import Callable

@dataclass
class Deps:
    thing_loader: Callable[..., None] | None = None
"""
    f = _write_module(tmp_path, src, name="deps_loader_field.py")
    violations = file_violations(f)
    assert any("thing_loader" in v for v in violations)


# ---------------------------------------------------------------------------
# Sanity coverage — clean shape, non-matching suffix, no-default kwargs
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_clean_function_with_no_test_seam_returns_no_violations(tmp_path: Path) -> None:
    """A function with no matching kwarg shows no violations."""
    src = """
def normal_function(x, y=1, *, kw=None):
    return x + y
"""
    f = _write_module(tmp_path, src, name="clean.py")
    assert file_violations(f) == []


@pytest.mark.unit
def test_non_matching_suffix_is_not_flagged(tmp_path: Path) -> None:
    """``thing_handle=None`` — ``_handle`` is not in the broadened suffix
    set, so the kwarg passes. Pins the suffix list as the gate.
    """
    src = """
def _resolve_thing(*, thing_handle=None):
    return thing_handle
"""
    f = _write_module(tmp_path, src, name="non_match.py")
    assert file_violations(f) == []


@pytest.mark.unit
def test_kwarg_with_non_none_default_is_not_flagged(tmp_path: Path) -> None:
    """``thing_loader=DEFAULT_LOADER`` — non-None default; the kwarg has
    a real production-time value, not a test-only seam shape.
    """
    src = """
DEFAULT_LOADER = lambda: "default"

def resolve_thing(*, thing_loader=DEFAULT_LOADER):
    return thing_loader()
"""
    f = _write_module(tmp_path, src, name="non_none_default.py")
    assert file_violations(f) == []


@pytest.mark.unit
def test_positional_with_default_loader_kwarg_is_flagged(tmp_path: Path) -> None:
    """Positional-with-default — the loader is passed positionally but
    defaults to None. Same smell, different signature shape. Pins the
    positional path of the detector (not just kw-only).
    """
    src = """
def _resolve_thing(arg, thing_loader=None):
    return thing_loader() if thing_loader else arg
"""
    f = _write_module(tmp_path, src, name="positional_default.py")
    violations = file_violations(f)
    assert any(v.endswith("::_resolve_thing::thing_loader") for v in violations)


@pytest.mark.unit
def test_async_function_with_loader_kwarg_is_flagged(tmp_path: Path) -> None:
    """``async def _resolve_thing(*, thing_loader=None)`` is the same shape
    on an ``AsyncFunctionDef`` node; the detector walks both function
    flavours.
    """
    src = """
async def _resolve_thing(*, thing_loader=None):
    return thing_loader() if thing_loader else "default"
"""
    f = _write_module(tmp_path, src, name="async_priv.py")
    violations = file_violations(f)
    assert any(v.endswith("::_resolve_thing::thing_loader") for v in violations)


# ---------------------------------------------------------------------------
# Public surface — file_has_violation is the historical boolean entry
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_file_has_violation_thin_boolean_wrapper(tmp_path: Path) -> None:
    """``file_has_violation`` returns a bool — kept for callers that need
    a yes/no answer without the per-param detail.
    """
    src_clean = "def normal(): pass\n"
    src_dirty = "def _resolve(*, thing_loader=None): return thing_loader\n"
    f_clean = _write_module(tmp_path, src_clean, name="clean_bool.py")
    f_dirty = _write_module(tmp_path, src_dirty, name="dirty_bool.py")
    assert file_has_violation(f_clean) is False
    assert file_has_violation(f_dirty) is True


@pytest.mark.unit
def test_syntax_error_returns_no_violations(tmp_path: Path) -> None:
    """A file that fails to parse returns ``[]`` — the detector treats
    broken sources as non-applicable rather than crashing the gate.
    """
    src = "def broken(:::"
    f = _write_module(tmp_path, src, name="broken.py")
    assert file_violations(f) == []


# ---------------------------------------------------------------------------
# Suffix set itself — pin the broadening
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_test_seam_suffixes_contains_broadened_set() -> None:
    """``_TEST_SEAM_SUFFIXES`` carries the full broadened set documented
    in the module docstring. Pins the broadening — if the suffix set is
    accidentally narrowed (e.g. someone reverts to just ``("_fn",)``),
    this test fails immediately.
    """
    assert "_fn" in _TEST_SEAM_SUFFIXES
    assert "_loader" in _TEST_SEAM_SUFFIXES
    assert "_factory" in _TEST_SEAM_SUFFIXES
    assert "_builder" in _TEST_SEAM_SUFFIXES
    assert "_provider" in _TEST_SEAM_SUFFIXES
    assert "_resolver" in _TEST_SEAM_SUFFIXES
