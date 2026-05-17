"""F1 detector: flag tests that substitute kairix-internal implementations.

Walks every test file via AST and reports the path of any file that
matches one of six shapes:

1. ``@patch("kairix.X.Y", ...)`` — decorator
2. ``with patch("kairix.X.Y", ...):`` — context manager
3. ``kairix.X.Y = <expr>`` — full-path attribute assignment
4. ``<alias>.Y = <expr>`` where ``<alias>`` resolves to a kairix module
5. ``monkeypatch.setattr("kairix.X.Y", ...)`` — string-target form
6. ``monkeypatch.setattr(<kairix module ref>, "attr", fake)`` — ref-target form

Stdlib roots (``os``, ``time``, ``pathlib``, ``sys``, ``importlib``,
``builtins``, ``threading``, ``functools``, ``re``, ``json``,
``logging``, ...) and external SDK roots (``httpx``, ``openai``,
``boto3``, ``anthropic``, ``requests``, ``numpy``, ``neo4j``,
``usearch``, ``sentence_transformers``, ``spacy``, ``rich``,
``click``, ``unittest``, ``pytest``) are exempt — patching these is
fixturing genuinely external state at the kairix edge.

To extend with a new shape: add the detection branch to
``file_has_internal_patch`` and add the matching positive + negative
tests to ``tests/architecture/test_check_no_internal_patches.py``.

Output: one violation file path per line on stdout, sorted,
deduplicated. Pipes into ``arch_gate`` from ``_lib.sh`` for baseline
diff.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REMEDIATION = """Refactor to constructor injection with a fake from tests/fakes.py to pass.

fix: rewrite the test to construct the unit under test with a Fake*
from tests/fakes.py (e.g. ``SearchPipeline(retriever=FakeRetriever(...))``).
If the production class lacks a constructor seam, add one — same shape
as ``GoldBuilder(llm_judge=, retriever=, db_path=)``.

When production resolves dependencies via function-local imports at
call time, move that resolution to construction time via the existing
``*Deps`` dataclass with ``default_factory`` — see
``EmbedDependencies`` / ``LLMBackendDeps`` / ``BenchmarkDeps`` for the
canonical shape, then inject the Fake* at construction.

next: re-run ``python3 scripts/checks/check_no_internal_patches.py``
to confirm the gate goes green.
run: bash scripts/safe-commit.sh "refactor(<area>): inject Fake via DI seam"

Pass example:
  pipeline = SearchPipeline(retriever=FakeRetriever(hits=[...]))
  assert pipeline.run(query='x') == ...

Shapes that fire the gate (all six are the same anti-pattern):
  @patch('kairix.core.search.bm25.bm25_search')
  with patch('kairix.providers.get_provider'):
  kairix.paths.provider_name = lambda: "fake"
  paths_mod.provider_name = lambda: "fake"
  monkeypatch.setattr("kairix.paths.provider_name", ...)
  monkeypatch.setattr(kairix.paths, "provider_name", ...)

Stdlib boundaries (os.*, time.*, etc.) and external SDK boundaries
(httpx.*, openai.*, boto3.*, etc.) remain allowed — F1 only flags
kairix.* targets."""


# Exempt module roots — stdlib and external SDKs whose patching is a
# legitimate boundary fake, not an internals violation.
_EXEMPT_ROOTS = frozenset(
    {
        # Stdlib
        "os",
        "sys",
        "time",
        "pathlib",
        "importlib",
        "builtins",
        "threading",
        "functools",
        "re",
        "json",
        "logging",
        "asyncio",
        "subprocess",
        "shutil",
        "tempfile",
        "datetime",
        "collections",
        "io",
        "ast",
        "typing",
        "contextlib",
        "warnings",
        "uuid",
        "hashlib",
        "hmac",
        "secrets",
        "struct",
        "copy",
        "itertools",
        # External SDKs
        "httpx",
        "openai",
        "boto3",
        "anthropic",
        "requests",
        "numpy",
        "yaml",
        "ruamel",
        "neo4j",
        "usearch",
        "sentence_transformers",
        "spacy",
        "rich",
        "click",
        # Testing infra
        "unittest",
        "pytest",
        "mock",
    }
)


def _resolve_kairix_aliases(tree: ast.AST) -> dict[str, str]:
    """Map local name -> fully-qualified kairix path from the file's imports.

    Examples:
      ``import kairix.paths as paths_mod`` -> {"paths_mod": "kairix.paths"}
      ``from kairix import providers as p`` -> {"p": "kairix.providers"}
      ``from kairix.paths import provider_name`` -> {"provider_name": "kairix.paths.provider_name"}
      ``import kairix.paths`` -> {"kairix": "kairix"}  (root binding only)
    """
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not alias.name.startswith("kairix"):
                    continue
                if alias.asname:
                    aliases[alias.asname] = alias.name
                else:
                    # `import kairix.paths` binds `kairix` (not `kairix.paths`)
                    # in the local namespace; track that so `kairix.paths.X = ...`
                    # is detected.
                    aliases[alias.name.split(".")[0]] = alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod != "kairix" and not mod.startswith("kairix."):
                continue
            for alias in node.names:
                local = alias.asname or alias.name
                # `from kairix.paths import provider_name`
                #   -> {"provider_name": "kairix.paths.provider_name"}
                # `from kairix import providers as p`
                #   -> {"p": "kairix.providers"}
                aliases[local] = f"{mod}.{alias.name}" if mod else f"kairix.{alias.name}"
    return aliases


def _attribute_root_name(node: ast.expr) -> str | None:
    """For an Attribute chain like ``a.b.c``, return ``"a"`` (the leftmost Name)."""
    cur = node
    while isinstance(cur, ast.Attribute):
        cur = cur.value
    if isinstance(cur, ast.Name):
        return cur.id
    return None


def _resolves_to_kairix(expr: ast.expr, aliases: dict[str, str]) -> bool:
    """Does ``expr`` (a Name or Attribute) resolve to a kairix module?

    True when:
      - ``expr`` is a Name whose id is in the alias map and points at
        a kairix qualified path
      - ``expr`` is an Attribute whose root is the literal name ``kairix``
        or an alias of one
    """
    if isinstance(expr, ast.Name):
        if expr.id == "kairix":
            return True
        return expr.id in aliases and aliases[expr.id].startswith("kairix")
    if isinstance(expr, ast.Attribute):
        root = _attribute_root_name(expr)
        if root is None:
            return False
        if root == "kairix":
            return True
        if root in _EXEMPT_ROOTS:
            return False
        return root in aliases and aliases[root].startswith("kairix")
    return False


def _is_patch_call(node: ast.expr) -> bool:
    """Return True when ``node`` is a Call to ``patch`` / ``mock.patch`` etc.

    Conservative: only matches the literal name ``patch`` or attribute
    access ending in ``.patch``. Other helpers (``patch.dict``,
    ``patch.object``) have a different arg shape and are NOT covered by F1.
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "patch"
    if isinstance(func, ast.Attribute):
        return func.attr == "patch"
    return False


def _first_arg_is_kairix_string(call: ast.Call) -> bool:
    """First positional arg of patch(...) / setattr(...) is a string starting with ``kairix.``."""
    if not call.args:
        return False
    first = call.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value == "kairix" or first.value.startswith("kairix.")
    return False


def _is_monkeypatch_setattr(node: ast.Call) -> bool:
    """``node`` is ``monkeypatch.setattr(...)``."""
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "setattr"
        and isinstance(func.value, ast.Name)
        and func.value.id == "monkeypatch"
    )


def file_has_internal_patch(path: Path) -> bool:
    """Return True iff ``path`` contains any of the six F1 violation shapes."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, OSError):
        return False

    aliases = _resolve_kairix_aliases(tree)

    for node in ast.walk(tree):
        # Shape 1: @patch decorator on kairix target
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for deco in node.decorator_list:
                if _is_patch_call(deco) and _first_arg_is_kairix_string(deco):
                    return True

        # Shape 2: with patch(...) on kairix target
        if isinstance(node, ast.With):
            for item in node.items:
                ctx = item.context_expr
                if _is_patch_call(ctx) and _first_arg_is_kairix_string(ctx):
                    return True

        # Shape 3 + 4: attribute assignment ``<...>.attr = expr`` where root
        # resolves to a kairix module
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Attribute) and _resolves_to_kairix(target, aliases):
                    return True

        # Shape 5 + 6: monkeypatch.setattr(...) on kairix target
        if isinstance(node, ast.Call) and _is_monkeypatch_setattr(node):
            if _first_arg_is_kairix_string(node):
                return True
            if node.args and _resolves_to_kairix(node.args[0], aliases):
                return True

    return False


def main() -> int:
    root = Path("tests")
    if not root.is_dir():
        return 0

    violators: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if file_has_internal_patch(path):
            violators.append(str(path))

    for v in violators:
        print(v)
    return 0


if __name__ == "__main__":
    sys.exit(main())
