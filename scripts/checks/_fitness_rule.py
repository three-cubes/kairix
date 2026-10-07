"""FitnessRule ABC (ADR-026 Track B).

A thin convenience layer over :mod:`tc_fitness` that lets a check
declare itself as a 3-line subclass:

.. code-block:: python

    class F44(FitnessRule):
        name = "f44-engagement-firm-boundary"
        remediation = REMEDIATION
        roots = ("kairix",)

        def file_has_violation(self, path: Path) -> bool:
            ...

    if __name__ == "__main__":
        raise SystemExit(F44().run())

The body of every concrete check stays focused on ``file_has_violation``
— the only thing that genuinely varies per rule. Enumerating files,
applying scope predicates, failing on any violation (there is no
baseline / grandfathering), and writing the F21 remediation are
inherited from the base class via :func:`tc_fitness.gate`.

Existing functional helpers (:func:`tc_fitness.gate`,
:func:`tc_fitness.main_entry`, :func:`tc_fitness.python_files`) remain
the canonical low-level API. The ABC does not replace them — it
collapses the boilerplate around them. Checks that need custom
enumeration, two-pass scans, or external input
sources stay as plain functions calling the helpers directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import ClassVar

# Shared gating helpers, sourced from the installed three-cubes-fitness package.
from tc_fitness import REPO_ROOT, gate


class FitnessRule(ABC):
    """Concrete subclasses declare class attributes + one detection method.

    Class attributes (required):
        name: gate name used in the ``ok`` / ``FAIL [arch:<name>]`` verdict
        remediation: F21-compliant ``fix:`` / ``next:`` / ``run:`` + Pass / Forbidden examples

    Class attributes (optional, with defaults):
        roots: tuple of repo-relative directories to scan (default: ``("kairix",)``)
        extensions: filename extensions to include (default: ``(".py",)``)

    There is no per-file exemption list: every in-scope file is checked.

    Concrete method (required):
        :meth:`file_has_violation`: return truthy when the file violates the rule

    Optional overrides:
        :meth:`is_in_scope`: customise the scope predicate
        :meth:`enumerate_files`: customise file enumeration (default uses :func:`python_files`)
    """

    name: ClassVar[str]
    remediation: ClassVar[str]
    roots: ClassVar[tuple[str, ...]] = ("kairix",)
    extensions: ClassVar[tuple[str, ...]] = (".py",)

    def __init__(self, repo_root: Path | None = None) -> None:
        """``repo_root`` overrides the default :data:`REPO_ROOT`. Tests pass
        a tmp_path for isolation; production callers omit and get the
        canonical repository root.
        """
        self._repo_root: Path = repo_root if repo_root is not None else REPO_ROOT

    @abstractmethod
    def file_has_violation(self, path: Path) -> bool:
        """Return True when the file at ``path`` violates this rule."""

    def is_in_scope(self, rel: str) -> bool:
        """Default scope predicate: under one of ``roots`` AND ending in
        one of ``extensions``. Override for non-standard scope shapes
        (e.g. ``.feature`` files, single-file scans).
        """
        return any(rel.startswith(prefix) and rel.endswith(self.extensions) for prefix in self.roots)

    def enumerate_files(self) -> list[Path]:
        """Default file enumeration: rglob each root for files matching
        ``extensions``, skipping ``__pycache__``. Override for non-standard
        enumeration (single-file scans, Gherkin parsing, etc.).
        """
        out: list[Path] = []
        for root in self.roots:
            root_path = self._repo_root / root
            if not root_path.exists():
                continue
            for path in root_path.rglob("*"):
                if not path.is_file():
                    continue
                if "__pycache__" in path.parts:
                    continue
                if any(path.name.endswith(ext) for ext in self.extensions):
                    out.append(path)
        return out

    def _repo_relative(self, path: Path) -> Path:
        """Repo-relative path. Resolves symlinks and tolerates either
        absolute or already-relative inputs.
        """
        if path.is_absolute():
            try:
                return path.resolve().relative_to(self._repo_root)
            except ValueError:
                pass
        return path

    def collect_violations(self, repo_root: Path | None = None) -> set[Path]:
        """Walk in-scope files; return the set of repo-relative paths that
        :meth:`file_has_violation` flags. No file is exempt.

        ``repo_root`` override exists for back-compat with existing tests
        that pass ``tmp_path`` for isolation. New code constructs the
        subclass with ``MyRule(repo_root=tmp_path)`` instead.
        """
        if repo_root is not None:
            return type(self)(repo_root=repo_root).collect_violations()
        out: set[Path] = set()
        for path in self.enumerate_files():
            rel_path = self._repo_relative(path)
            rel = str(rel_path)
            if not self.is_in_scope(rel):
                continue
            if self.file_has_violation(path):
                out.add(rel_path)
        return out

    def run(self) -> int:
        """Fail on any violation (no baseline). Return the exit code."""
        return gate(self.name, self.collect_violations(), self.remediation)
