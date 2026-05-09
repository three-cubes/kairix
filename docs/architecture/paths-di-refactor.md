# Paths in kairix

Paths are resolved **once at the boundary** into an immutable `KairixPaths`
value object. Inner code calls module-level convenience functions
(`document_root()`, `db_path()`, `log_dir()`, `workspace_root()`) which
read from the cached resolution. Tests override paths by setting
`KAIRIX_*` environment variables and clearing the resolution cache.

## The shape

```python
@dataclass(frozen=True)
class KairixPaths:
    document_root: Path
    db_path: Path
    log_dir: Path
    workspace_root: Path

    @classmethod
    def resolve(cls) -> KairixPaths: ...
```

Resolution order, highest first:

1. `KAIRIX_*` environment variables (`KAIRIX_DOCUMENT_ROOT`, `KAIRIX_DB_PATH`,
   `KAIRIX_LOG_DIR`, `KAIRIX_WORKSPACE_ROOT`).
2. `paths:` section of `kairix.config.yaml`.
3. Platform-aware defaults — Docker, system service install, or per-user XDG.

Resolution is `lru_cache(maxsize=1)`-d inside `_resolve_cached()`. The cache
is cleared by `kairix.paths.clear_cache()`.

## Production usage

Inner code does not take a `paths` parameter. It calls the convenience
function it needs:

```python
from kairix.paths import document_root, db_path

def some_helper() -> None:
    root = document_root()         # reads KairixPaths.resolve().document_root
    db = sqlite3.connect(db_path()) # reads KairixPaths.resolve().db_path
```

Each convenience function delegates to `KairixPaths.resolve()`, which is
cached, so multiple calls within a process pay one env-read.

`agent_memory_path(agent)` is the same pattern: it reads
`KAIRIX_AGENT_MEMORY_ROOT` from the process environment and falls back to
`document_root() / "04-Agent-Knowledge" / agent / "memory"`. The
path-doubling guard (#67 / #93 regression) lives inside the function.

## Test usage

Tests override paths via `monkeypatch.setenv` and `clear_cache()`:

```python
from kairix.paths import clear_cache, document_root

def test_thing(tmp_path, monkeypatch):
    monkeypatch.setenv("KAIRIX_DOCUMENT_ROOT", str(tmp_path / "vault"))
    clear_cache()
    assert document_root() == tmp_path / "vault"
```

For tests that need a constructed `KairixPaths` value — e.g. unit tests of
business logic that takes a `KairixPaths` parameter via constructor —
`tests.fakes.FakePaths()` returns a real `KairixPaths` from explicit
arguments:

```python
from tests.fakes import FakePaths

def test_pipeline(tmp_path):
    paths = FakePaths(
        document_root=tmp_path / "vault",
        workspace_root=tmp_path / "ws",
    )
    pipeline = WikiLinksAuditor(paths=paths)
```

`FakePaths` is a constructor helper, not a separate type — it returns a
real `KairixPaths`. Production and tests share the same value type.

## Where to read paths

- **CLI / factory / MCP build_server**: at startup, before constructing
  pipelines or repositories. The result lives in the constructed object.
- **Long-running classes** (e.g. `SearchPipeline`, `EmbedPipeline`): take a
  `paths: KairixPaths` argument in `__init__` if they need paths beyond
  what their other dependencies already encapsulate.
- **Stateless helpers**: call the convenience function directly. The cache
  makes repeated calls free.
